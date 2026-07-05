#!/bin/bash
# Run ON the Pi from ~/proximity-player: bash install.sh
set -e
cd "$(dirname "$0")"

sudo apt-get update
sudo apt-get install -y --no-install-recommends mpv python3-opencv ffmpeg

# Placeholder test videos if the real ones aren't there yet
mkdir -p videos
# PAL-sized placeholders (720x576 @ 25fps) for the composite output
if [ ! -f videos/idle.mp4 ]; then
  ffmpeg -y -f lavfi -i "smptebars=size=720x576:rate=25" \
         -f lavfi -i "sine=frequency=220" -t 10 \
         -c:v libx264 -preset veryfast -c:a aac -shortest videos/idle.mp4
fi
if [ ! -f videos/trigger.mp4 ]; then
  ffmpeg -y -f lavfi -i "testsrc2=size=720x576:rate=25" \
         -f lavfi -i "sine=frequency=880" -t 8 \
         -vf "drawtext=text='TRIGGERED':fontsize=90:fontcolor=white:x=(w-tw)/2:y=(h-th)/2" \
         -c:v libx264 -preset veryfast -c:a aac -shortest videos/trigger.mp4
fi

# systemd service (headless: mpv renders via DRM/KMS, no desktop needed)
RUN_USER="${SUDO_USER:-$USER}"
cat > /tmp/proximity-player.service <<EOF
[Unit]
Description=Proximity-triggered video player
# NOT multi-user.target: a WantedBy=multi-user unit that is also After= it
# makes an ordering cycle; systemd silently drops jobs to break it.
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$(pwd)
ExecStart=/usr/bin/python3 $(pwd)/player.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo cp /tmp/proximity-player.service /etc/systemd/system/proximity-player.service

# auto-mount the first USB stick at /media/usb (videos: idle.<ext>, trigger.<ext>)
sudo mkdir -p /media/usb
grep -q media/usb /etc/fstab || \
  echo "/dev/sda1 /media/usb auto ro,nofail,noauto,x-systemd.automount,x-systemd.idle-timeout=30 0 0" | sudo tee -a /etc/fstab >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now proximity-player.service
sleep 3
systemctl status proximity-player.service --no-pager | head -12
