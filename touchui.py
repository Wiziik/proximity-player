#!/usr/bin/env python3
"""Touchscreen settings panel for the proximity player.

Draws a 480x320 control UI straight to the fbtft framebuffer (MPI3501
3.5" SPI panel, tft35a overlay) and reads taps from the ADS7846 touch
controller via evdev. Talks to the player's local HTTP API (player.py,
port 8080) so all settings logic lives in one place.

Run `touchui.py calibrate` to (re)do the 5-point touch calibration;
the affine matrix is saved to calib.json next to this file.
"""

import json
import os
import sys
import time
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
CALIB_FILE = os.path.join(BASE, "calib.json")
API = "http://127.0.0.1:8080"
W, H = 480, 320
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ------------- framebuffer -------------


def find_fb():
    """Pick the fbtft framebuffer (not the vc4/composite one)."""
    for i in range(4):
        name_f = f"/sys/class/graphics/fb{i}/name"
        try:
            with open(name_f) as f:
                name = f.read().strip().lower()
        except OSError:
            continue
        if "ili" in name or "tft" in name or "fb_" in name:
            return f"/dev/fb{i}"
    if os.path.exists("/dev/fb1"):
        return "/dev/fb1"
    return "/dev/fb0"


FB = find_fb()


def blit(img):
    """PIL RGB image -> RGB565 -> framebuffer."""
    a = np.asarray(img, dtype=np.uint16)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    with open(FB, "wb") as f:
        f.write(rgb565.astype("<u2").tobytes())


# ------------- touch -------------


def find_touch():
    from evdev import InputDevice, list_devices
    for path in list_devices():
        dev = InputDevice(path)
        if "ADS7846" in dev.name or "ads7846" in dev.name:
            return dev
    return None


def load_calib():
    try:
        with open(CALIB_FILE) as f:
            return np.array(json.load(f))
    except (OSError, ValueError):
        # Rough default for tft35a rotate=90: raw X maps to screen X
        # inverted, raw Y to screen Y inverted. Big buttons forgive this;
        # run `touchui.py calibrate` for accuracy.
        return np.array([[-W / 4095.0, 0.0, W],
                         [0.0, -H / 4095.0, H]])


def raw_to_screen(m, rx, ry):
    x = m[0][0] * rx + m[0][1] * ry + m[0][2]
    y = m[1][0] * rx + m[1][1] * ry + m[1][2]
    return int(x), int(y)


def read_tap(dev, timeout=0.05):
    """Return (raw_x, raw_y) on touch-down, else None. Non-blocking."""
    from evdev import ecodes
    from select import select
    r, _, _ = select([dev.fd], [], [], timeout)
    if not r:
        return None
    x = y = None
    down = False
    for ev in dev.read():
        if ev.type == ecodes.EV_ABS:
            if ev.code == ecodes.ABS_X:
                x = ev.value
            elif ev.code == ecodes.ABS_Y:
                y = ev.value
        elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH:
            down = ev.value == 1
    if down and x is not None and y is not None:
        return (x, y)
    return None


def wait_tap_blocking(dev):
    while True:
        t = read_tap(dev, timeout=1.0)
        if t:
            # swallow the release + drag events
            time.sleep(0.4)
            try:
                while dev.read_one():
                    pass
            except BlockingIOError:
                pass
            return t


def calibrate(dev):
    """5-point affine calibration, least squares, saved to calib.json."""
    pts = [(40, 40), (W - 40, 40), (W - 40, H - 40), (40, H - 40),
           (W // 2, H // 2)]
    font = ImageFont.truetype(FONT, 20)
    raws = []
    for px, py in pts:
        img = Image.new("RGB", (W, H), "black")
        d = ImageDraw.Draw(img)
        d.text((W // 2 - 90, H // 2 - 40), "touch the cross",
               font=font, fill="#888")
        d.line((px - 12, py, px + 12, py), fill="red", width=3)
        d.line((px, py - 12, px, py + 12), fill="red", width=3)
        blit(img)
        raws.append(wait_tap_blocking(dev))
    A = np.array([[rx, ry, 1] for rx, ry in raws])
    m = []
    for i in (0, 1):
        tgt = np.array([p[i] for p in pts])
        sol, *_ = np.linalg.lstsq(A, tgt, rcond=None)
        m.append(list(sol))
    with open(CALIB_FILE, "w") as f:
        json.dump(m, f)
    print("calibration saved:", m)
    return np.array(m)


# ------------- player API -------------


def api_get(path):
    try:
        with urllib.request.urlopen(API + path, timeout=1.5) as r:
            return json.load(r)
    except OSError:
        return None


def api_post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(API + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return json.load(r)
    except OSError:
        return None


# ------------- UI -------------

# label, key, step, decimals
ROWS = [
    ("Trigger",  "proximity_ratio", 0.01, 2),
    ("Debounce", "consec_frames",   1,    0),
    ("Cooldown", "cooldown_s",      1,    0),
    ("Live",     "live_s",          1,    0),
]
ROW_Y0, ROW_H = 96, 38
BTN_W = 64


def draw(status, flash=None):
    img = Image.new("RGB", (W, H), "#101010")
    d = ImageDraw.Draw(img)
    f16 = ImageFont.truetype(FONT, 16)
    f20 = ImageFont.truetype(FONT, 20)
    f26 = ImageFont.truetype(FONT, 26)

    if status is None:
        d.text((20, 20), "PLAYER OFFLINE", font=f26, fill="#f55")
        d.text((20, 60), "waiting for player.py API...", font=f16, fill="#888")
        blit(img)
        return

    st = status.get("state", "?")
    cam = status.get("camera", False)
    ratio = status.get("ratio", 0.0)
    s = status["settings"]

    d.text((12, 8), "PROXIMITY", font=f20, fill="#ddd")
    col = {"idle": "#5d5", "playing": "#fa3", "cooldown": "#5af"}.get(st, "#888")
    d.text((160, 8), st.upper(), font=f20, fill=col)
    if not cam:
        d.text((300, 8), "NO CAM", font=f20, fill="#f55")

    # ratio bar, 0..0.5 scale, red threshold tick
    bx, by, bw, bh = 12, 44, W - 24, 34
    d.rectangle((bx, by, bx + bw, by + bh), fill="#222")
    fill_w = int(min(1.0, ratio / 0.5) * bw)
    hot = ratio >= s["proximity_ratio"]
    d.rectangle((bx, by, bx + fill_w, by + bh),
                fill="#c53" if hot else "#295")
    tx = bx + int(s["proximity_ratio"] / 0.5 * bw)
    d.rectangle((tx - 1, by, tx + 1, by + bh), fill="#f66")
    d.text((bx + 8, by + 7), f"{ratio:.3f}", font=f16, fill="#eee")

    for i, (label, key, step, dec) in enumerate(ROWS):
        y = ROW_Y0 + i * ROW_H
        val = s[key]
        d.text((12, y + 8), label, font=f16, fill="#aaa")
        # minus button
        d.rounded_rectangle((150, y + 2, 150 + BTN_W, y + ROW_H - 4),
                            6, fill="#333" if flash != (key, -1) else "#48f")
        d.text((150 + BTN_W // 2 - 7, y + 4), "−", font=f26, fill="#fff")
        # value
        d.text((240, y + 8), f"{val:.{dec}f}", font=f20, fill="#8cf")
        # plus button
        d.rounded_rectangle((330, y + 2, 330 + BTN_W, y + ROW_H - 4),
                            6, fill="#333" if flash != (key, +1) else "#48f")
        d.text((330 + BTN_W // 2 - 8, y + 4), "+", font=f26, fill="#fff")

    # test button
    ty = ROW_Y0 + len(ROWS) * ROW_H + 2
    d.rounded_rectangle((12, ty, 200, H - 6), 8,
                        fill="#48f" if flash == "test" else "#264")
    d.text((52, ty + 6), "TEST", font=f26, fill="#fff")
    ip = os.popen("hostname -I").read().split()
    d.text((220, ty + 10), f"http://{ip[0] if ip else '?'}:8080",
           font=f16, fill="#777")

    blit(img)


def hit(x, y):
    """Map a screen tap to an action."""
    for i, (label, key, step, dec) in enumerate(ROWS):
        ry = ROW_Y0 + i * ROW_H
        if ry <= y <= ry + ROW_H:
            if 130 <= x <= 150 + BTN_W + 20:
                return ("adj", key, -step)
            if 310 <= x <= 330 + BTN_W + 20:
                return ("adj", key, +step)
    ty = ROW_Y0 + len(ROWS) * ROW_H + 2
    if y >= ty and x <= 210:
        return ("test",)
    return None


def main():
    dev = None
    while dev is None:
        dev = find_touch()
        if dev is None:
            print("no ADS7846 touch device yet, retrying...")
            time.sleep(3)

    if len(sys.argv) > 1 and sys.argv[1] == "calibrate":
        calibrate(dev)
        return

    m = load_calib()
    status = None
    last_poll = 0.0
    dirty = True
    last_tap_t = 0.0
    last_draw = 0.0

    while True:
        now = time.time()
        if now - last_poll > 0.3:
            last_poll = now
            new = api_get("/api/status")
            if new != status:
                status = new
                dirty = True

        tap = read_tap(dev, timeout=0.05)
        if tap and status and now - last_tap_t > 0.25:   # debounce
            last_tap_t = now
            x, y = raw_to_screen(m, *tap)
            act = hit(x, y)
            if act:
                if act[0] == "adj":
                    _, key, delta = act
                    val = round(status["settings"][key] + delta, 4)
                    res = api_post("/api/settings", {key: val})
                    if res:
                        status["settings"] = res
                    draw(status, flash=(key, 1 if delta > 0 else -1))
                    time.sleep(0.08)
                elif act[0] == "test":
                    api_post("/api/test-trigger")
                    draw(status, flash="test")
                    time.sleep(0.15)
                dirty = True

        # periodic repaint too: recovers from anything else writing to
        # the framebuffer, and keeps the ratio bar visibly live
        if dirty or now - last_draw > 2.0:
            draw(status)
            dirty = False
            last_draw = now


if __name__ == "__main__":
    main()
