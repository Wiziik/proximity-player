#!/bin/bash
# Enable the MPI3501 3.5" GPIO touchscreen (ILI9486 + XPT2046) and install
# the touchui settings panel service. Run as root ON THE PI. Idempotent.
#
# The stock `piscreen` overlay leaves this panel WHITE/blank — it needs the
# goodtft tft35a overlay's init sequence (same fix as the WaybackProxy Pi).
# The panel appears as a second framebuffer next to the composite/vc4 one;
# mpv's --vo=drm output is unaffected.
set -e

CFG=/boot/firmware/config.txt
OVL=/boot/firmware/overlays/tft35a.dtbo
DTBO_URL=https://raw.githubusercontent.com/goodtft/LCD-show/master/usr/tft35a-overlay.dtb

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

if [ ! -f "$OVL" ]; then
    echo "downloading tft35a overlay..."
    curl -fsSL -o "$OVL" "$DTBO_URL"
fi

cp -n "$CFG" "$CFG.pre-touchscreen.bak"
grep -q '^dtparam=spi=on' "$CFG" || echo 'dtparam=spi=on' >> "$CFG"
grep -q '^dtoverlay=tft35a' "$CFG" || echo 'dtoverlay=tft35a,rotate=90' >> "$CFG"

apt-get install -y python3-evdev python3-pil python3-numpy fonts-dejavu-core

cat > /etc/systemd/system/proximity-touchui.service <<'EOF'
[Unit]
Description=Proximity player touchscreen settings panel
After=proximity-player.service

[Service]
WorkingDirectory=/home/pi/proximity-player
ExecStart=/usr/bin/python3 /home/pi/proximity-player/touchui.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable proximity-touchui.service

echo "done — reboot to bring up the panel (then: systemctl status proximity-touchui)"
echo "if touch is inaccurate: systemctl stop proximity-touchui && python3 touchui.py calibrate"
echo "if the panel is blank/white with no touch: RESEAT the HAT firmly (esp. middle pins)"
