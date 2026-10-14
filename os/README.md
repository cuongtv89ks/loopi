# Loopi OS (Buildroot, Raspberry Pi Zero W)

## Build
`./os/build.sh image` → `os/out/loopi-dev.img` (needs Docker; first build 1–3 h, later builds minutes).

## Flash and configure
1. `sudo dd if=os/out/loopi-dev.img of=/dev/sdX bs=4M conv=fsync status=progress`
   (or Raspberry Pi Imager → "Use custom").
2. On the `boot` partition: copy `loopi.toml.example` to `loopi.toml` and fill in WiFi, the brain URL,
   `pair_token` and `ca_fingerprint` (brain web → Thiết bị → "Tạo token ghép cặp").
3. Dev images: put your SSH public key in `boot/loopi-authorized_keys`.
4. Insert the card and connect the Pi's `USB` port (not `PWR`) to your computer with a micro-USB data
   cable. Dev images start in USB gadget mode: the cable powers the Pi and gives a console
   (`screen /dev/cu.usbmodem* 115200`) and network (`ssh root@10.55.0.1`).

Reflashing wipes `/data`, so every flash needs a fresh `pair_token` (revoke the old device on the brain).

On first boot the device grows the data partition, imports `loopi.toml` (secrets are then removed
from the boot partition), joins WiFi, pairs with the brain and starts recording (LED solid).

## USB port modes (one per boot)
- `gadget` (default on dev images): console + network to your computer; no microphone.
- `host`: the microphone goes into `USB` (OTG adapter if needed) and the charger into `PWR`; reach the
  device over WiFi (`ssh root@<wifi-ip>`).

Switch with `loopi-usbmode host` or `loopi-usbmode gadget`, then `reboot`. Without SSH, edit `config.txt`
on the boot partition: `# ` in front of `dtoverlay=dwc2,dr_mode=peripheral` means host mode.

## Develop the agent without reflashing
`os/scripts/deploy-agent.sh <device-ip>` rebuilds the ARMv6 binary and runs it from `/data/dev/`.
Remove `/data/dev/loopi-agent` on the device to go back to the packaged binary.

## Useful commands on the device
`loopi-agent selftest` · `loopi-usbmode` · `journalctl -u loopi-agent -f` · `systemctl --failed` · `rpicam-vid --list-cameras` · `arecord -l`
