# Loopi OS (Buildroot, Raspberry Pi Zero W)

## Build
`VARIANT=dev|prod os/build.sh image` (default `dev`) → `os/out/loopi-<variant>-<version>.img`,
`os/out/loopi-<variant>-<version>.squashfs` and the signed update bundle `os/out/loopi-<variant>-<version>.raucb` with its
description `.json` (see "OTA" below; needs Docker; first build 1–3 h, later builds minutes).

- **Two variants** (spec §6.5). `dev` keeps SSH (key-only), the USB gadget console, `loopi-usbmode`, rsync, strace,
  the ALSA tools and the `/data/dev/loopi-agent` override; its extra files live in
  `board/loopi/rootfs-overlay-dev/` and `configs/dev.fragment`. `prod` has none of them: no SSH server, the USB port is
  always host, no debug tools beyond `loopi-agent selftest`, and `run-agent` only ever starts `/usr/bin/loopi-agent`
  (`configs/prod.fragment`). Everything else is shared (`configs/loopi.fragment`, `board/loopi/rootfs-overlay/`).
  `check-image.sh` detects the variant from `/etc/loopi-dev` and runs the dev-only checks on dev images only.
- **Two output directories**, one per variant, in the `loopi-buildroot` volume: `/br/output` (dev, built since P1) and
  `/br/output-prod`. Buildroot does not rebuild correctly when the fragments change under an existing output
  directory, so a variant never reuses the other's. The first prod build starts from scratch (the download cache is
  shared). `post-build.sh` stops the build if the variant and the `/etc/loopi-dev` marker disagree.
- **Version stamp**: every build is `<UTC date>-<7-char commit>` (e.g. `2026.10.19-f1cb4c9`), with `-dirty` appended when
  the git tree has uncommitted changes (`publish-bundle.sh` refuses those, so a bundle always names a real commit).
  It is written to `/etc/loopi-release` (`LOOPI_OS_VERSION=`, `LOOPI_OS_VARIANT=`), which the agent reports in every
  heartbeat. `LOOPI_BROKEN=agent|kernel` makes the deliberately broken builds for the OTA acceptance test; they are
  versioned `...-broken-<what>` and skip the image checks.
- **OTA signing keys**: `os/build.sh image` refuses to run unless `~/.config/loopi/rauc/` holds `ca.cert.pem`,
  `signing.cert.pem` and `signing.key.pem` (create them once with `os/scripts/rauc-keys.sh`; `LOOPI_RAUC_KEYS=<dir>`
  points elsewhere). The images carry the CA certificate and the bundles are signed with the signing key. The keys are
  never in git.

## Flash and configure
1. `sudo dd if=os/out/loopi-<variant>-<version>.img of=/dev/sdX bs=4M conv=fsync status=progress`
   (or Raspberry Pi Imager → "Use custom").
2. On the `boot` partition: copy `loopi.toml.example` to `loopi.toml` and fill in WiFi, the brain URL,
   `pair_token` and `ca_fingerprint` (brain web → Thiết bị → "Tạo token ghép cặp"; the fingerprint is on the same
   page). Do not skip `ca_fingerprint`: without it the device trusts the first server it reaches when it pairs.
3. Dev images: put your SSH public key in `boot/loopi-authorized_keys`. `loopi-devkeys` copies it over
   `/data/dev/ssh/authorized_keys` at every boot, so edit the boot copy (a key added only on the device is gone after
   the next boot) and delete the file to keep the device's own list.
4. Prod images: put the microphone in `USB` (OTG adapter if needed), power the Pi from `PWR` and let it join WiFi from
   `loopi.toml`; there is no SSH and no USB console. Dev images: insert the card and connect the Pi's `USB` port (not
   `PWR`) to your computer with a micro-USB data cable. Dev images start in USB gadget mode: the cable powers the Pi
   and gives a console and network (`ssh root@10.55.0.1`). The console is `/dev/ttyACM0` on Linux (`screen /dev/ttyACM0 115200`) and
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

To change it on a running device, edit `/data/config/loopi.toml` (the only file the agent reads at run time), set
`audio_gain_db = <dB>` (a number from 0 to 36) under `[capture]` (add the table if the file has none), and run
`systemctl restart loopi-agent` (or reboot): the agent reads its config once at start. `/boot/loopi.toml` is imported
only at the first boot, while it still holds the WiFi passphrase or pair token, so later edits there are ignored. A value
outside 0-36 or a misspelt key makes the agent refuse to start (nothing is recorded, LED off), so check
`journalctl -u loopi-agent -n 20` afterwards.

## USB port modes (one per boot)
Dev images only; a prod image has no `loopi-usbmode` and the USB port is always host (microphone in `USB`, charger in `PWR`).
- `gadget` (default on dev images): console + network to your computer; no microphone.
- `host`: the microphone goes into `USB` (OTG adapter if needed) and the charger into `PWR`; reach the
  device over WiFi (`ssh root@<wifi-ip>`).

Switch with `loopi-usbmode host` or `loopi-usbmode gadget`, then `reboot`. On the running device `/boot` is
read-only (see below), so do not edit `config.txt` there by hand: `loopi-usbmode` opens it for the edit and closes
it again. Without SSH, put the card in a computer and edit `config.txt` on the boot partition: `# ` in front of
`dtoverlay=dwc2,dr_mode=peripheral` means host mode.

## Boot partition and power cuts
The device loses power without warning, and a FAT volume that was open for writing is then left dirty. So `/boot`
(`config.txt`, the slot directories `a/` and `b/` with the kernel and `cmdline.txt`, `loopi.toml`) is mounted
read-only and is writable only while `loopi-provision` runs at boot (the first boot imports `loopi.toml` and removes the
secrets from it), while `loopi-usbmode` edits `config.txt` and while an update writes a slot (`loopi-boot`, see "OTA").
At every boot `/boot` (`fsck.fat`) and `/data` (`e2fsck`) are checked and repaired before they are
mounted (`journalctl -u systemd-fsck@dev-mmcblk0p1`, `-u systemd-fsck@dev-mmcblk0p4`). `fsck.repair=yes` on the kernel
command line makes the repair answer "yes" to every question, so it never stops to ask; a file that a power cut had
already damaged may be deleted by it. If a partition cannot be repaired the device still boots without it (`nofail`):
reflash the card. To change a file in `/boot` by hand: `mount -o remount,rw /boot`, edit, `sync`,
`mount -o remount,ro /boot`.

## OTA (P4a)
Updates are RAUC bundles signed by a key that never leaves the build machine (spec D26-D28); a device installs one into
its idle rootfs slot, boots it once as a trial and keeps it only when the agent confirms it within 10 minutes.

- **Keys**: create them once with `os/scripts/rauc-keys.sh` (`~/.config/loopi/rauc/`: `ca.cert.pem`, `ca.key.pem`,
  `signing.cert.pem`, `signing.key.pem`; `LOOPI_RAUC_KEYS=<dir>` points elsewhere). **Back that directory up**: without it
  no update can ever be installed again, only a re-flash. It is never in git and the script never prints it. Every image
  carries only `ca.cert.pem` (as `/etc/rauc/keyring.pem`); the bundles are signed with `signing.key.pem`.
- **Bundles**: every `os/build.sh image` signs one, `os/out/loopi-<variant>-<version>.raucb`, and writes
  `os/out/loopi-<variant>-<version>.json` (`bundle`, `version`, `variant`, `sha256`, `size`, `built_at`, `commit`), the
  description the brain lists the bundle by. The bundle holds the same `rootfs.squashfs` as the image (`compatible=loopi-rpi0w`,
  verity format). Publish one to the brain with `os/scripts/publish-bundle.sh`.
- **RAUC is a command-line tool only** (`BR2_PACKAGE_RAUC_DBUS` is not set): the image has no `rauc.service` and no
  `rauc-mark-good.service`. The latter would confirm every trial boot by itself and so defeat the rollback;
  `check-image.sh` fails the build if either appears. It also checks the kernel config (loop, device-mapper, dm-verity,
  gzip squashfs, SHA-256) and that the shared `cmdline.txt` template takes `root=` and `rauc.slot=` from the slot.
- **Boot partition** (FAT32, 64 MiB): the firmware files (`bootcode.bin`, `start.elf`, `fixup.dat`), `loopi.toml.example`,
  and
  - `config.txt`: boots the good slot (`os_prefix=a/` or `b/`; a fresh image has `a/`). Written only by renaming
    `tryboot.txt` over it, never from nothing.
  - `tryboot.txt`: exists only during an update. The firmware reads it on a `0 tryboot` reboot only, so a trial boots the
    new slot and any other reboot (watchdog, `panic=10`, power loss, the 10-minute guard) falls back to `config.txt`.
  - `a/` and `b/`: one slot each (`/dev/mmcblk0p2` = A, `/dev/mmcblk0p3` = B): `zImage`, the device tree, `overlays/`
    (with its `README`, without which the firmware ignores the directory), `cmdline.txt` (with `rauc.slot=` and `root=` of
    that slot) and `loopi-config.txt` (the config template the slot was built with). `b/` is empty until the first update.
  `loopi-boot` (`/usr/libexec/loopi/loopi-boot`) lays these out, both in the image build and on the device (the files it
  copies are staged in every rootfs under `/usr/share/loopi/boot/`).
- **Dev images only** carry the USB gadget line (`board/loopi/config-dev.txt`, appended to the template); on prod the USB
  port is never a gadget.
- **A dev agent deployed to `/data/dev/loopi-agent`** (`os/scripts/deploy-agent.sh`) replaces the rootfs agent on dev
  images, so it must be OTA-aware (built from a tree that has the P4a agent): a trial boot is confirmed by the running
  agent, and an old binary never does, so the update would roll back after 10 minutes. Remove `/data/dev/loopi-agent`
  before an update to test the packaged agent.

## Develop the agent without reflashing
Dev images only. `os/scripts/deploy-agent.sh <device-ip>` rebuilds the ARMv6 binary and runs it from `/data/dev/`.
Remove `/data/dev/loopi-agent` on the device to go back to the packaged binary.

## Debugging a prod device
A prod image has no SSH, no `loopi-usbmode` and no tools besides `loopi-agent selftest`. To debug one, install a dev
bundle over OTA from the brain web (dev and prod bundles share the signing key and `compatible`, so either can be
installed over the other, spec D28): SSH is back, key-only, for keys already in `/data/dev/ssh`. Install a prod bundle
again when done.

## Useful commands on the device
`journalctl -u loopi-agent -f` · `systemctl --failed` · `rpicam-vid --list-cameras` · `cat /etc/loopi-release`
Dev images also have `loopi-usbmode` and `arecord -l`.

`loopi-agent selftest` needs the agent stopped (only one process can open the microphone):
`systemctl stop loopi-agent && loopi-agent selftest; systemctl start loopi-agent`. Start the agent again afterwards:
nothing is recorded while it is stopped.
