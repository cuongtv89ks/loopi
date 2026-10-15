# Loopi OS (Buildroot, Raspberry Pi Zero W)

## Build
`./os/build.sh image` → `os/out/loopi-dev.img` (needs Docker; first build 1–3 h, later builds minutes).

## Flash and configure
1. `sudo dd if=os/out/loopi-dev.img of=/dev/sdX bs=4M conv=fsync status=progress`
   (or Raspberry Pi Imager → "Use custom").
2. On the `boot` partition: copy `loopi.toml.example` to `loopi.toml` and fill in WiFi, the brain URL,
   `pair_token` and `ca_fingerprint` (brain web → Thiết bị → "Tạo token ghép cặp"; the fingerprint is on the same
   page). Do not skip `ca_fingerprint`: without it the device trusts the first server it reaches when it pairs.
3. Dev images: put your SSH public key in `boot/loopi-authorized_keys`. `loopi-devkeys` copies it over
   `/data/dev/ssh/authorized_keys` at every boot, so edit the boot copy (a key added only on the device is gone after
   the next boot) and delete the file to keep the device's own list.
4. Insert the card and connect the Pi's `USB` port (not `PWR`) to your computer with a micro-USB data
   cable. Dev images start in USB gadget mode: the cable powers the Pi and gives a console and network
   (`ssh root@10.55.0.1`). The console is `/dev/ttyACM0` on Linux (`screen /dev/ttyACM0 115200`) and
   `/dev/cu.usbmodem*` on macOS. On Linux, ModemManager may grab the port for a few seconds after it appears; keep it
   away with a udev rule, e.g. in `/etc/udev/rules.d/99-loopi.rules`, then `sudo udevadm control --reload`:
   `SUBSYSTEM=="tty", ATTRS{idVendor}=="1d6b", ATTRS{idProduct}=="0104", ENV{ID_MM_DEVICE_IGNORE}="1"`

Reflashing wipes `/data`, so every flash needs a fresh `pair_token` (revoke the old device on the brain). It also
gives the device a new SSH host key: drop the old one with `ssh-keygen -R 10.55.0.1` (and the WiFi address).

On first boot the device grows the data partition, imports `loopi.toml` (secrets are then removed
from the boot partition), joins WiFi, pairs with the brain and starts recording (LED solid).

## Privacy LED
The recording light is the Pi's green ACT LED (spec D18), driven only by the agent: solid while anything records, slow
blink while paused or in quiet hours, fast blink on an error (not paired, no NTP, disk full, camera failing), off when
the agent is not running. The kernel does not drive it after boot (`dtparam=act_led_trigger=none`), and systemd switches
it off after every exit of the agent, also a crash or a kill (`loopi-agent led-off`). If the LED cannot be driven the
agent records nothing. An external LED on a GPIO instead: `[led]` `source = "gpio"` in `loopi.toml` (line `[gpio]`
`led`, default 17).

## Microphone level
The USB microphone records speech very quietly (about -55 dBFS RMS, measured on two days of recordings with its own
automatic gain control on; raising its mixer volume adds only 2-3 dB, and the image leaves the mixer at its power-on
settings). So the agent amplifies every captured frame by `[capture]` `audio_gain_db` in `loopi.toml` (default 20 dB,
0-36) before encoding, through a limiter that holds loud sounds at -1 dBFS instead of letting them clip (spec D19).
`loopi-agent selftest` prints the level of one second before and after the gain. If speech in the room still reads far
below -30 dBFS after the gain, raise it; if the limiter flattens normal speech, lower it. The brain normalises the level
again for transcription and playback, so the raw files only need to be loud enough to keep speech above the encoder's
noise.

## USB port modes (one per boot)
- `gadget` (default on dev images): console + network to your computer; no microphone.
- `host`: the microphone goes into `USB` (OTG adapter if needed) and the charger into `PWR`; reach the
  device over WiFi (`ssh root@<wifi-ip>`).

Switch with `loopi-usbmode host` or `loopi-usbmode gadget`, then `reboot`. On the running device `/boot` is
read-only (see below), so do not edit `config.txt` there by hand: `loopi-usbmode` opens it for the edit and closes
it again. Without SSH, put the card in a computer and edit `config.txt` on the boot partition: `# ` in front of
`dtoverlay=dwc2,dr_mode=peripheral` means host mode.

## Boot partition and power cuts
The device loses power without warning, and a FAT volume that was open for writing is then left dirty. So `/boot`
(`config.txt`, `cmdline.txt`, kernel, `loopi.toml`) is mounted read-only and is writable only while `loopi-provision`
runs at boot (the first boot imports `loopi.toml` and removes the secrets from it) and while `loopi-usbmode` edits
`config.txt`. At every boot `/boot` (`fsck.fat`) and `/data` (`e2fsck`) are checked and repaired before they are
mounted (`journalctl -u systemd-fsck@dev-mmcblk0p1`, `-u systemd-fsck@dev-mmcblk0p4`). `fsck.repair=yes` on the kernel
command line makes the repair answer "yes" to every question, so it never stops to ask; a file that a power cut had
already damaged may be deleted by it. If a partition cannot be repaired the device still boots without it (`nofail`):
reflash the card. To change a file in `/boot` by hand: `mount -o remount,rw /boot`, edit, `sync`,
`mount -o remount,ro /boot`.

## Develop the agent without reflashing
`os/scripts/deploy-agent.sh <device-ip>` rebuilds the ARMv6 binary and runs it from `/data/dev/`.
Remove `/data/dev/loopi-agent` on the device to go back to the packaged binary.

## Useful commands on the device
`loopi-usbmode` · `journalctl -u loopi-agent -f` · `systemctl --failed` · `rpicam-vid --list-cameras` · `arecord -l`

`loopi-agent selftest` needs the agent stopped (only one process can open the microphone):
`systemctl stop loopi-agent && loopi-agent selftest; systemctl start loopi-agent`. Start the agent again afterwards:
nothing is recorded while it is stopped.
