# Proximity-triggered video player

Raspberry Pi 4 kiosk for an installation: loops an **idle** video on a PAL
composite TV; when the webcam sees someone approach, switches to the
**live webcam feed** (a mirror) for a settable duration, then returns to
the idle loop.

Deployed on the Pi 4 (Rev 1.4, hostname `pi`, Wi-Fi) on the LAN,
login `pi`/`pi`. Careful: there is a second, identical-looking Pi 4 at
.108 (an older composite-SECAM video box) — don't deploy there.

## How it works

- `player.py` runs as the `proximity-player` systemd service (headless
  Raspberry Pi OS Lite, no desktop). mpv renders straight to KMS/DRM with
  `--vo=drm --hwdec=no` — **do not** switch to `--vo=gpu --gpu-context=drm`:
  it plays one file fine but crashes on every file swap on the composite
  output (`DRM_IOCTL_MODE_CREATE_DUMB: Cannot allocate memory`).
- Proximity = OpenCV MOG2 background subtraction on the webcam
  (Logitech C920, `/dev/video0`, 320x240@10fps). When moving foreground
  fills ≥ `PROXIMITY_RATIO` of the frame for `CONSEC_FRAMES` frames, mpv
  switches to the live feed (`av://v4l2:/dev/video0`) via its IPC socket
  (`/tmp/proximity-mpv.sock`) for `LIVE_S` seconds, then idle resumes
  after a `COOLDOWN_S` re-arm delay.
- The camera has a single opener: OpenCV releases it before mpv takes it,
  so **no detection happens during the live view** (fixed duration).
- **mpv segfault gotcha:** mpv crashes tearing down the v4l2 demuxer
  ("Some buffers are still owned by the caller on close"), with raw and
  MJPEG capture alike. Never swap away from the live stream — the player
  quits and respawns mpv to return to idle (~1 s of black), and also
  respawns it if it ever dies on its own.
- The player never dies to a black screen: missing webcam → keeps looping
  idle and retries every 5 s; unexpected end-of-file → reloads idle;
  mpv exit → systemd restarts everything.

## USB stick

Two files at the root of the stick (FAT/exFAT/NTFS), any of
.mp4/.mov/.mkv/.avi/.m4v:

```
idle.mp4       loops forever
```

(`trigger.mp4` is no longer used — proximity now shows the live webcam.)

At startup the player copies them to `~/proximity-player/cache/` on the SD
card and plays from there (the stick is slow — ~15 s to open a file while
the webcam hogs the USB bus — and this also lets you remove the stick after
boot). To change videos: new files on the stick, power-cycle the Pi; it
re-copies when file sizes differ. Best encode for PAL: 720x576, 25 fps.

The stick automounts read-only at `/media/usb` via fstab
(`x-systemd.automount`, `nofail`).

## Composite PAL output

`setup-composite-pal.sh` sets `enable_tvout=1`, `sdtv_mode=2` (PAL),
`sdtv_aspect=1`, adds `composite=1` to the `vc4-kms-v3d` overlay, and puts
`video=Composite-1:720x576@50ie vc4.tv_norm=PAL` on the kernel cmdline.
Video comes out of the 3.5 mm TRRS jack (4-pole, camcorder pinout).
**HDMI goes dark while composite is enabled** — Pi 4 limitation.

## Install on a fresh Pi

```bash
scp -r proximity-player pi@<ip>:~/
ssh pi@<ip> 'sudo bash ~/proximity-player/install.sh'
ssh pi@<ip> 'sudo bash ~/proximity-player/setup-composite-pal.sh'
ssh pi@<ip> 'sudo reboot'
```

`install.sh` installs mpv/python3-opencv/ffmpeg, generates 720x576 test
videos (SMPTE bars idle / "TRIGGERED" testsrc) as fallbacks, writes the
systemd unit, and adds the USB automount. Run scripts as root (`sudo -S
bash script`); don't wrap `sudo` in a password-piping shell function — it
feeds the password into heredocs' stdin and corrupts written files.

## Tuning

Constants at the top of `player.py` (or env vars on the service):

| Setting | Default | Meaning |
|---|---|---|
| `PROXIMITY_RATIO` | 0.15 | Fraction of frame that must move. Higher = must be closer. |
| `CONSEC_FRAMES` | 3 | Debounce frames above threshold. |
| `COOLDOWN_S` | 5 | Re-arm delay after the live view ends. |
| `LIVE_S` | 15 | How long the live mirror stays on screen. |
| `CAM_INDEX` | 0 | Webcam device number. |

Watch it live (each trigger logs the ratio it saw):

```bash
ssh pi@<pi-ip> journalctl -u proximity-player -f
```

## Settings UI (web + GPIO touchscreen)

`player.py` serves a touch-friendly control panel on **http://<pi-ip>:8080/**
(phone/tablet/laptop): live camera-ratio bar with threshold marker, sliders
for the tunables above, and a test-trigger button. Changes apply live
and persist to `settings.json` (which wins over env-var defaults).
API: `GET/POST /api/settings`, `GET /api/status`, `POST /api/test-trigger`.

The same panel runs on the MPI3501 3.5" GPIO touchscreen (ILI9486 +
XPT2046): `touchui.py` draws to the fbtft framebuffer (`/dev/fb1`,
RGB565 480x320) and reads ADS7846 taps via evdev, driving the local API.
Runs as systemd `proximity-touchui.service` (root). Setup (done):
`sudo bash setup-touchscreen.sh` — installs the goodtft `tft35a` overlay
(stock `piscreen` leaves this panel white), `dtparam=spi=on` +
`dtoverlay=tft35a,rotate=90`, python3-{evdev,pil,numpy}, and the unit.

Gotchas:
- The SPI panel coexists fine with composite: vc4 keeps `/dev/fb0`/DRM
  (mpv untouched), fbtft gets `/dev/fb1`.
- **Ordering cycle:** a unit `WantedBy=multi-user.target` must NOT also be
  `After=multi-user.target` — systemd silently deletes a job to break the
  cycle (that's why touchui skipped at boot until proximity-player.service
  was changed to `After=network.target`).
- Touch inaccurate? `sudo systemctl stop proximity-touchui &&
  sudo python3 touchui.py calibrate` (5-point, saved to calib.json).
- Panel white / touch dead: reseat the HAT firmly (backlight works on
  power alone; data pins need even pressure).
