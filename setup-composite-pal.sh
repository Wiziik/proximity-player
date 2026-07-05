#!/bin/bash
# Configure the Pi to output on the composite jack in PAL. Run ON the Pi.
# Requires a reboot to take effect. NOTE: on Pi 4, composite and HDMI are
# mutually exclusive -- after reboot the HDMI port goes dark.
set -e

CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
CMD=/boot/firmware/cmdline.txt
[ -f "$CMD" ] || CMD=/boot/cmdline.txt

echo "config: $CFG"
sudo cp "$CFG" "$CFG.bak.$(date +%s)"

add_cfg() {  # add_cfg key=value  (only if not already present)
  grep -q "^$1" "$CFG" || echo "$1" | sudo tee -a "$CFG" >/dev/null
}

# Legacy firmware settings (harmless under KMS, required on older stacks)
add_cfg "enable_tvout=1"      # Pi 4: composite is off by default
add_cfg "sdtv_mode=2"         # 2 = normal PAL
add_cfg "sdtv_aspect=1"       # 4:3

# KMS (Bookworm): the vc4 overlay needs composite=1
if grep -q "^dtoverlay=vc4-kms-v3d" "$CFG" && \
   ! grep -q "^dtoverlay=vc4-kms-v3d.*composite" "$CFG"; then
  sudo sed -i 's/^dtoverlay=vc4-kms-v3d.*/&,composite=1/' "$CFG"
fi

# Tell the kernel to bring the composite connector up in PAL (576i50)
sudo cp "$CMD" "$CMD.bak.$(date +%s)"
if ! grep -q "Composite-1" "$CMD"; then
  sudo sed -i '1 s/$/ video=Composite-1:720x576@50ie/' "$CMD"
fi
if grep -q "vc4.tv_norm=" "$CMD"; then
  sudo sed -i 's/vc4.tv_norm=[A-Za-z-]*/vc4.tv_norm=PAL/' "$CMD"
else
  sudo sed -i '1 s/$/ vc4.tv_norm=PAL/' "$CMD"
fi

echo "--- $CFG (relevant lines) ---"
grep -E "tvout|sdtv|vc4-kms" "$CFG"
echo "--- $CMD ---"
cat "$CMD"
echo
echo "Done. Reboot to switch to composite PAL: sudo reboot"
