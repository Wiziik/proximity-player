#!/usr/bin/env python3
"""Proximity-triggered video player with live-mirror trigger.

Loops videos/idle.mp4 fullscreen in mpv. A webcam is watched with OpenCV
background subtraction; when something large (= close) enters the frame,
the screen switches to the LIVE webcam feed for live_s seconds (a mirror),
then the idle loop resumes. The camera is handed over to mpv during the
live view (v4l2 = one opener), so no detection happens while it shows.

Tune the CONFIG block below, or override with env vars of the same name.
Live-tunable settings are exposed on a touch-friendly web panel at
http://<pi>:8080/ and persisted to settings.json (which wins over env).
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------- CONFIG ----------------
BASE = os.path.dirname(os.path.abspath(__file__))
USB_DIR = "/media/usb"          # auto-mounted first USB stick (see fstab)
USB_DEV = "/dev/sda1"
CACHE_DIR = os.path.join(BASE, "cache")   # SD copy of the USB videos
MPV_SOCK = "/tmp/proximity-mpv.sock"
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".m4v")

CAM_INDEX = int(os.environ.get("CAM_INDEX", 0))      # /dev/video0
CAM_W, CAM_H = 320, 240                              # low res is plenty
DETECT_FPS = 10                                      # detection rate

WARMUP_S = 5.0                                            # let bg model settle
UI_PORT = int(os.environ.get("UI_PORT", 8080))
SETTINGS_FILE = os.path.join(BASE, "settings.json")

# Live-tunable settings: (env default, min, max, step, label).
# proximity_ratio: fraction of the frame that must be "foreground" to count
# as proximity. Bigger = person must be closer. 0.05 sensitive, 0.30 close.
# live_s: how long the live webcam "mirror" view stays on screen. There is
# no presence detection during it — mpv owns the camera (one opener only),
# so the duration is fixed.
SETTING_DEFS = {
    "proximity_ratio": (float(os.environ.get("PROXIMITY_RATIO", 0.15)),
                        0.01, 0.50, 0.01, "Trigger threshold"),
    "consec_frames":   (int(os.environ.get("CONSEC_FRAMES", 3)),
                        1, 10, 1, "Debounce frames"),
    "cooldown_s":      (float(os.environ.get("COOLDOWN_S", 5.0)),
                        0, 60, 1, "Cooldown (s)"),
    "live_s":          (float(os.environ.get("LIVE_S", 15.0)),
                        3, 120, 1, "Live view (s)"),
}
SETTINGS = {k: v[0] for k, v in SETTING_DEFS.items()}
SETTINGS_LOCK = threading.Lock()

# Live status shared with the web UI, and its "test trigger" request.
STATUS = {"state": "starting", "ratio": 0.0, "camera": False}
TEST_TRIGGER = threading.Event()
# -----------------------------------------


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    with SETTINGS_LOCK:
        for k, v in saved.items():
            if k in SETTING_DEFS:
                lo, hi = SETTING_DEFS[k][1], SETTING_DEFS[k][2]
                SETTINGS[k] = min(hi, max(lo, float(v)))


def save_settings():
    with SETTINGS_LOCK:
        data = dict(SETTINGS)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def setting(k):
    with SETTINGS_LOCK:
        return SETTINGS[k]


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def find_video(name):
    """Resolve 'idle'/'trigger' to a file: SD cache of the USB videos first
    (fast, survives stick removal), then the USB stick itself, then the
    bundled videos/ folder. Only touch the (auto)mount point when the
    device actually exists, otherwise systemd would block waiting for it."""
    dirs = [CACHE_DIR]
    if os.path.exists(USB_DEV):
        dirs.append(USB_DIR)
    dirs.append(os.path.join(BASE, "videos"))
    for d in dirs:
        for ext in VIDEO_EXTS:
            p = os.path.join(d, name + ext)
            if os.path.isfile(p):
                return p
    return None


def sync_usb_cache():
    """Copy idle/trigger from the USB stick to the SD card. The stick is
    slow (~15s just to open a file while the webcam hogs the USB bus), so
    playback always uses the local copy. Skips unchanged files."""
    if not os.path.exists(USB_DEV):
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    for name in ("idle", "trigger"):
        for ext in VIDEO_EXTS:
            src = os.path.join(USB_DIR, name + ext)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(CACHE_DIR, name + ext)
            try:
                if (not os.path.isfile(dst)
                        or os.path.getsize(dst) != os.path.getsize(src)):
                    log(f"caching {src} -> SD")
                    shutil.copyfile(src, dst + ".tmp")
                    os.replace(dst + ".tmp", dst)
                # drop stale cache copies with a different extension
                for other in VIDEO_EXTS:
                    if other != ext:
                        stale = os.path.join(CACHE_DIR, name + other)
                        if os.path.isfile(stale):
                            os.unlink(stale)
            except OSError as e:
                log(f"cache copy failed: {e}")
            break


class Mpv:
    """Minimal mpv JSON IPC wrapper."""

    def __init__(self):
        if os.path.exists(MPV_SOCK):
            os.unlink(MPV_SOCK)
        # Headless Pi OS Lite: render straight to KMS/DRM (no X/Wayland).
        # vo=drm + software decode: the GL/GBM path crashes on file swap
        # with the composite output (CREATE_DUMB ENOMEM); PAL 576i is light
        # enough to do entirely in software.
        self.proc = subprocess.Popen([
            "mpv",
            "--vo=drm", "--hwdec=no",
            "--fs", "--no-osc", "--no-osd-bar",
            "--no-input-default-bindings", "--no-terminal",
            "--keep-open=no", "--idle=yes", "--force-window=yes",
            f"--input-ipc-server={MPV_SOCK}",
        ])
        for _ in range(100):
            if os.path.exists(MPV_SOCK):
                break
            time.sleep(0.1)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(MPV_SOCK)
        self.sock.settimeout(0.05)
        self.buf = b""

    def cmd(self, *args):
        payload = json.dumps({"command": list(args)}) + "\n"
        self.sock.sendall(payload.encode())

    def events(self):
        """Yield any pending mpv events (non-blocking)."""
        try:
            self.buf += self.sock.recv(65536)
        except socket.timeout:
            pass
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "event" in msg:
                yield msg

    def play_idle(self):
        v = find_video("idle")
        if v:
            self.cmd("loadfile", v, "replace")
            self.cmd("set_property", "loop-file", "inf")
        else:
            log("no idle video found!")

    def play_live(self):
        """Fullscreen live view of the webcam. Caller must have released
        the OpenCV capture first — v4l2 allows a single opener."""
        self.cmd("set_property", "loop-file", "no")
        self.cmd("loadfile", f"av://v4l2:/dev/video{CAM_INDEX}", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


UI_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Proximity Player</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin: 0; padding: 16px; background: #111; color: #eee;
         font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
         max-width: 560px; margin-inline: auto; }
  h1 { font-size: 1.2rem; margin: 0 0 4px; }
  #state { color: #8f8; font-size: 1rem; min-height: 1.2em; }
  .bar { position: relative; height: 44px; background: #222;
         border-radius: 8px; overflow: hidden; margin: 12px 0 20px; }
  #fill { position: absolute; inset: 0; width: 0%; background: #295;
          transition: width .15s linear; }
  #thresh { position: absolute; top: 0; bottom: 0; width: 3px;
            background: #f66; }
  #ratio { position: absolute; inset: 0; display: flex; align-items: center;
           justify-content: center; font-variant-numeric: tabular-nums; }
  .row { margin-bottom: 18px; }
  label { display: flex; justify-content: space-between;
          font-size: 1rem; margin-bottom: 6px; }
  label output { font-variant-numeric: tabular-nums; color: #8cf; }
  input[type=range] { width: 100%; height: 44px; accent-color: #48f; }
  button { width: 100%; height: 56px; font-size: 1.1rem; border: 0;
           border-radius: 10px; background: #48f; color: #fff;
           margin-top: 8px; }
  button:active { background: #26c; }
  #saved { text-align: center; color: #888; min-height: 1.2em;
           font-size: .9rem; margin-top: 10px; }
</style></head><body>
<h1>Proximity Player</h1>
<div id="state">connecting&hellip;</div>
<div class="bar"><div id="fill"></div><div id="thresh"></div>
  <div id="ratio">0.00</div></div>
<div id="sliders"></div>
<button id="test">Test trigger</button>
<div id="saved"></div>
<script>
const DEFS = __DEFS__;
const sliders = document.getElementById('sliders');
let touching = false, saveTimer = null;

for (const [k, d] of Object.entries(DEFS)) {
  const row = document.createElement('div');
  row.className = 'row';
  row.innerHTML = `<label>${d.label}<output id="o_${k}"></output></label>
    <input type="range" id="s_${k}" min="${d.min}" max="${d.max}"
           step="${d.step}" value="${d.value}">`;
  sliders.appendChild(row);
  const inp = row.querySelector('input'), out = row.querySelector('output');
  const show = () => out.textContent = (+inp.value).toFixed(d.step < 1 ? 2 : 0);
  show();
  inp.addEventListener('pointerdown', () => touching = true);
  inp.addEventListener('input', () => { show(); queueSave(); });
  inp.addEventListener('change', () => { touching = false; queueSave(); });
}

function queueSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const body = {};
    for (const k of Object.keys(DEFS)) body[k] = +document.getElementById('s_' + k).value;
    await fetch('/api/settings', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const el = document.getElementById('saved');
    el.textContent = 'saved';
    setTimeout(() => el.textContent = '', 1200);
  }, 350);
}

document.getElementById('test').addEventListener('click',
  () => fetch('/api/test-trigger', {method: 'POST'}));

async function poll() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('state').textContent =
      s.state + (s.camera ? '' : ' — NO CAMERA');
    document.getElementById('state').style.color = s.camera ? '#8f8' : '#f66';
    const pct = Math.min(100, s.ratio / 0.5 * 100);
    document.getElementById('fill').style.width = pct + '%';
    document.getElementById('ratio').textContent = s.ratio.toFixed(3);
    document.getElementById('fill').style.background =
      s.ratio >= s.settings.proximity_ratio ? '#c53' : '#295';
    document.getElementById('thresh').style.left =
      (s.settings.proximity_ratio / 0.5 * 100) + '%';
    if (!touching)
      for (const [k, v] of Object.entries(s.settings)) {
        const inp = document.getElementById('s_' + k);
        if (inp && document.activeElement !== inp) {
          inp.value = v;
          document.getElementById('o_' + k).textContent =
            (+v).toFixed(DEFS[k].step < 1 ? 2 : 0);
        }
      }
  } catch (e) {
    document.getElementById('state').textContent = 'connection lost';
    document.getElementById('state').style.color = '#f66';
  }
  setTimeout(poll, 400);
}
poll();
</script></body></html>
"""


class UiHandler(BaseHTTPRequestHandler):
    """Touch-friendly settings panel + JSON API, stdlib only."""

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            defs = {k: {"value": setting(k), "min": d[1], "max": d[2],
                        "step": d[3], "label": d[4]}
                    for k, d in SETTING_DEFS.items()}
            page = UI_HTML.replace("__DEFS__", json.dumps(defs))
            self._send(200, page.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            with SETTINGS_LOCK:
                snap = dict(SETTINGS)
            self._send(200, {**STATUS, "settings": snap})
        elif self.path == "/api/settings":
            with SETTINGS_LOCK:
                self._send(200, dict(SETTINGS))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/settings":
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
            except ValueError:
                self._send(400, {"error": "bad json"})
                return
            with SETTINGS_LOCK:
                for k, v in body.items():
                    if k in SETTING_DEFS:
                        lo, hi = SETTING_DEFS[k][1], SETTING_DEFS[k][2]
                        SETTINGS[k] = min(hi, max(lo, float(v)))
            save_settings()
            log("settings updated:", body)
            with SETTINGS_LOCK:
                self._send(200, dict(SETTINGS))
        elif self.path == "/api/test-trigger":
            TEST_TRIGGER.set()
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass  # keep journalctl clean


def start_ui():
    srv = ThreadingHTTPServer(("0.0.0.0", UI_PORT), UiHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"settings UI on http://0.0.0.0:{UI_PORT}/")


def open_camera(cv2):
    cap = cv2.VideoCapture(CAM_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_H)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open camera index {CAM_INDEX}")
    return cap


def main():
    import cv2

    load_settings()
    start_ui()
    sync_usb_cache()
    v = find_video("idle")
    if not v:
        sys.exit("no 'idle' video on USB or in videos/")
    log(f"idle: {v}")

    mpv = Mpv()
    mpv.play_idle()
    log("idle loop started")

    cap = None
    next_cam_try = 0.0
    bg = cv2.createBackgroundSubtractorMOG2(history=200, varThreshold=32,
                                            detectShadows=False)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    start = time.time()
    hot_frames = 0
    state = "idle"            # idle | live | cooldown
    cooldown_until = 0.0
    live_until = 0.0
    frame_interval = 1.0 / DETECT_FPS
    total_px = CAM_W * CAM_H

    def start_live():
        """Hand the camera over to mpv for the live mirror view."""
        nonlocal cap, state, live_until
        if cap is not None:
            cap.release()
            cap = None
        mpv.play_live()
        state = "live"
        live_until = time.time() + setting("live_s")

    def end_live(why):
        nonlocal mpv, state, cooldown_until, next_cam_try, start
        # mpv SEGFAULTS tearing down the v4l2 demuxer ("Some buffers are
        # still owned by the caller on close"), so never swap away from the
        # live stream — replace the whole mpv process instead.
        mpv.close()
        mpv = Mpv()
        mpv.play_idle()
        state = "cooldown"
        cooldown_until = time.time() + setting("cooldown_s")
        # give mpv a moment to close the device before OpenCV reopens it
        next_cam_try = time.time() + 2.0
        start = time.time()   # re-warm the bg model on reopen
        log(f"live view {why} -> idle + cooldown")

    while True:
        t0 = time.time()

        if mpv.proc.poll() is not None:
            if state == "live":
                end_live("mpv died")     # closes + respawns
            else:
                log("mpv died, respawning")
                mpv.close()
                mpv = Mpv()
                mpv.play_idle()

        # mpv events: live stream died (camera yanked/busy) -> back to idle
        for ev in mpv.events():
            if ev.get("event") == "end-file":
                reason = ev.get("reason", "")
                if reason not in ("eof", "error"):
                    continue
                if state == "live":
                    end_live(f"ended ({reason})")
                else:
                    # anything else ended (hiccup/external): never leave black
                    mpv.play_idle()
                    log(f"unexpected end-file ({reason}), reloading idle")

        # Web/touch-panel "test trigger" button: fire the live view now.
        if TEST_TRIGGER.is_set():
            TEST_TRIGGER.clear()
            if state != "live" and (cap is not None
                                    or os.path.exists(f"/dev/video{CAM_INDEX}")):
                log("test trigger -> live view")
                start_live()
                hot_frames = 0

        # Live mirror on screen: mpv owns the camera, just wait out the timer.
        if state == "live":
            STATUS.update(state=state, camera=True, ratio=0.0)
            if time.time() >= live_until:
                end_live("done")
            else:
                time.sleep(0.2)
            continue

        STATUS["state"] = state
        STATUS["camera"] = cap is not None

        # No camera yet (or it died): keep the idle loop running and retry.
        if cap is None:
            STATUS["ratio"] = 0.0
            if time.time() >= next_cam_try:
                next_cam_try = time.time() + 5.0
                if os.path.exists(f"/dev/video{CAM_INDEX}"):
                    try:
                        cap = open_camera(cv2)
                        start = time.time()  # re-warm the bg model
                        log(f"camera /dev/video{CAM_INDEX} opened")
                    except RuntimeError as e:
                        log(f"camera open failed: {e}")
            time.sleep(0.5)
            continue

        ok, frame = cap.read()
        if not ok:
            log("camera read failed, will reopen")
            cap.release()
            cap = None
            continue

        mask = bg.apply(frame)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        ratio = cv2.countNonZero(mask) / total_px
        STATUS["ratio"] = round(ratio, 4)

        if state == "cooldown" and time.time() >= cooldown_until:
            state = "idle"
            hot_frames = 0

        warmed_up = (time.time() - start) > WARMUP_S
        if state == "idle" and warmed_up:
            if ratio >= setting("proximity_ratio"):
                hot_frames += 1
            else:
                hot_frames = 0
            if hot_frames >= setting("consec_frames"):
                log(f"PROXIMITY (ratio={ratio:.2f}) -> live view")
                start_live()
                hot_frames = 0

        dt = time.time() - t0
        if dt < frame_interval:
            time.sleep(frame_interval - dt)


if __name__ == "__main__":
    main()
