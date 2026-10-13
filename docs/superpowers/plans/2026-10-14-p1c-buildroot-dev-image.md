# P1c — Buildroot Dev Image + Phase 1 Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce `os/out/loopi-dev.img`, a minimal Buildroot image for the Raspberry Pi Zero W (read-only squashfs root, A/B-ready partitions, systemd, WiFi from `loopi.toml`, kit CSI camera via libcamera, USB microphone, `loopi-agent` as a watchdog-supervised service, key-only SSH), then run the Phase 1 acceptance checks on real hardware against the P1a brain.

**Architecture:** A `BR2_EXTERNAL` tree in `os/` layered on Buildroot's upstream `raspberrypi0w_defconfig` through two config fragments (`loopi.fragment`, `dev.fragment`) merged with `merge_config.sh`, so kernel/firmware pins come from upstream while toolchain, init, filesystem and packages are ours. Builds run in a Debian container (works on x86_64 Linux and Apple Silicon); the source tree is copied into a named Docker volume so Buildroot gets a case-sensitive filesystem. `os/tests/check-image.sh` asserts the structure of every built image (the automated test for each task); on-device steps verify behaviour.

**Tech Stack:** Buildroot LTS (`2026.02.x`, fallback `2025.02.x`), internal glibc toolchain, Raspberry Pi kernel + firmware from the upstream defconfig, systemd (networkd, resolved, timesyncd), squashfs (lz4), genimage, libcamera (`rpi/vc4`) + `libcamera-apps` (rpicam-apps), alsa-lib, opus, wpa_supplicant, BlueZ, dropbear, host Rust (`host-rust-bin`), Docker.

**Spec:** `docs/superpowers/specs/2026-10-14-loopi-design.md` (§5, §6, §7.4, §12, §14 P1, §15 R1–R3, R8). **Depends on:** P1a (brain running for Tasks 3 and 6), P1b (agent sources in `agent/` with `Cargo.lock`).

## Global Constraints

- Buildroot ref: `BUILDROOT_REF=2026.02.x`; if that branch does not exist, `2025.02.x`. Base defconfig `raspberrypi0w_defconfig`; our changes only through `os/configs/*.fragment`, and `os/scripts/verify-config.sh` must pass (every fragment line present in the final `.config`).
- Internal Buildroot toolchain, glibc, C++ enabled, kernel headers 6.1 (≥ 5.5 for libcamera-apps, ≤ the Raspberry Pi kernel).
- Init: systemd. Root filesystem: squashfs (lz4), mounted read-only; `/var` and `/tmp` are tmpfs; journald volatile.
- SD layout (MBR): p1 `boot` FAT32 64 MiB · p2 `rootfs-A` squashfs 128 MiB · p3 `rootfs-B` 128 MiB empty · p4 `data` ext4 (64 MiB in the image, grown to the card on first boot).
- `config.txt`: `camera_auto_detect=1`, `dtoverlay=vc4-kms-v3d`, `dtparam=audio=off`, `enable_uart=1`; no `dtoverlay=dwc2` (the OTG port is a USB host for the mic).
- `cmdline.txt`: `root=/dev/mmcblk0p2 rootfstype=squashfs ro rootwait`.
- Agent service: `Type=notify`, `WatchdogSec=30`, `KillMode=control-group`, `Restart=always`; dev images prefer `/data/dev/loopi-agent` when present (marker `/etc/loopi-dev`).
- SSH: dropbear, key-only (`/data/dev/ssh/authorized_keys` imported from `boot/loopi-authorized_keys`), host keys persisted in `/data/dev/dropbear`.
- Only the dev variant exists in P1 (prod variant, RAUC and QR provisioning are P4).
- The agent is compiled by Buildroot (`generic-package` with `$(PKG_CARGO_ENV)`), which needs network access to crates.io during the build.
- Commits: conventional commits.

## Review Focus

1. Power cut during the first-boot data-partition grow → the next boot finishes the grow; the marker is written only after `resize2fs` succeeds and re-running the script is a no-op. Test: Task 2 Step 7.
2. A boot partition whose `loopi.toml` was already scrubbed (no passphrase, no token) → no re-import; WiFi keeps working from `/data/config`. Test: Task 3 Step 8.
3. Read-only root: services that normally write `/etc` (dropbear host keys, machine-id, resolv.conf) still start. Test: Task 5 Step 6.
4. Agent killed with SIGKILL → `rpicam-vid` must not keep recording with the LED off. Test: Task 6 Step 6.
5. Camera cable unplugged at boot → agent keeps running, LED fast-blinks after 5 failures, heartbeat shows camera failures, audio still records. Test: Task 4 Step 8.

---

## File Structure

```
os/
├── external.desc  external.mk  Config.in           # BR2_EXTERNAL "LOOPI"
├── configs/
│   ├── loopi.fragment                               # toolchain, systemd, fs, packages
│   └── dev.fragment                                 # SSH, debug tools, rsync
├── board/loopi/
│   ├── config.txt  cmdline.txt  genimage.cfg  linux.fragment
│   ├── loopi.toml.example
│   ├── post-build.sh  post-image.sh
│   └── rootfs-overlay/
│       ├── etc/fstab
│       ├── etc/loopi-dev
│       ├── etc/systemd/journald.conf.d/10-loopi.conf
│       ├── etc/systemd/system.conf.d/10-watchdog.conf
│       ├── etc/systemd/network/20-wlan0.network
│       ├── etc/systemd/system/{loopi-growdata,loopi-provision,loopi-wifi,loopi-agent,loopi-devkeys}.service
│       ├── etc/tmpfiles.d/loopi.conf
│       └── usr/libexec/loopi/{grow-data.sh,run-agent}
├── package/loopi-agent/{Config.in,loopi-agent.mk}
├── docker/Dockerfile
├── scripts/{container-build.sh,verify-config.sh,deploy-agent.sh}
├── tests/check-image.sh
├── build.sh
└── README.md
docs/measurements/2026-10-p1-acceptance.md          # Task 6
```

---

### Task 1: Builder container, external tree and a bootable base image

**Files:**
- Create: `os/docker/Dockerfile`, `os/build.sh`, `os/scripts/container-build.sh`, `os/scripts/verify-config.sh`, `os/tests/check-image.sh`, `os/external.desc`, `os/external.mk`, `os/Config.in`, `os/configs/loopi.fragment`, `os/configs/dev.fragment`, `os/board/loopi/{config.txt,cmdline.txt,genimage.cfg,linux.fragment,loopi.toml.example,post-build.sh,post-image.sh}`

**Interfaces:**
- Produces: `os/build.sh [image|agent|shell]` (image → `os/out/loopi-dev.img`, agent → `os/out/loopi-agent`); `os/tests/check-image.sh <images dir>` (prints `[ OK ]`/`[FAIL]` per check, exit 1 on any failure; later tasks add checks before the final `exit`); fragments consumed by `merge_config.sh`.

- [ ] **Step 1: Confirm the Buildroot branch exists**

Run: `git ls-remote --heads https://gitlab.com/buildroot.org/buildroot.git 2026.02.x`
Expected: one line ending in `refs/heads/2026.02.x`. If empty, use `2025.02.x` everywhere `2026.02.x` appears in this plan.

- [ ] **Step 2: Write the image checks first — `os/tests/check-image.sh`**

```bash
#!/usr/bin/env bash
# Structural checks on a built Loopi image. Usage: check-image.sh <buildroot images dir>
set -uo pipefail
IMAGES="${1:?usage: check-image.sh <images dir>}"
fail=0

check() {  # check <description> <command...>
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "[ OK ] $name"; else echo "[FAIL] $name"; fail=1; fi
}
rootfs_has() { unsquashfs -l "$IMAGES/rootfs.squashfs" | grep -Eq "^squashfs-root$1\$"; }
rootfs_cat() { unsquashfs -cat "$IMAGES/rootfs.squashfs" "$1"; }
rootfs_file_contains() { rootfs_cat "$1" | grep -Eq "$2"; }
boot_has() { mdir -i "$IMAGES/boot.vfat" "::$1"; }
part_count() { [ "$(sfdisk -d "$IMAGES/sdcard.img" | grep -c '^/')" = "$1" ]; }
part_type() { [ "$(sfdisk --part-type "$IMAGES/sdcard.img" "$1")" = "$2" ]; }

# --- Task 1: bootable base image
check "sdcard.img exists" test -s "$IMAGES/sdcard.img"
check "four partitions: boot, rootfs-A, rootfs-B, data" part_count 4
check "p1 is FAT32 (LBA)" part_type 1 c
check "p2 is Linux" part_type 2 83
check "p4 is Linux" part_type 4 83
check "kernel image present" test -s "$IMAGES/zImage"
check "systemd is the init" rootfs_has /usr/lib/systemd/systemd
check "cmdline mounts squashfs read-only" grep -q "root=/dev/mmcblk0p2 rootfstype=squashfs ro rootwait" "$IMAGES/rpi-firmware/cmdline.txt"
check "boot partition has config.txt" boot_has config.txt
check "boot partition has loopi.toml.example" boot_has loopi.toml.example

exit $fail
```

Run: `chmod +x os/tests/check-image.sh`

- [ ] **Step 3: Write the builder — `os/docker/Dockerfile`, `os/build.sh`, `os/scripts/container-build.sh`, `os/scripts/verify-config.sh`**

`os/docker/Dockerfile`:

```dockerfile
FROM debian:bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      bc bison build-essential bzip2 ca-certificates cpio dosfstools e2fsprogs fdisk file flex \
      git gzip libncurses-dev mtools patch perl python3 rsync squashfs-tools unzip util-linux \
      wget xz-utils \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 builder
ENV LANG=C.UTF-8
WORKDIR /br
```

`os/build.sh`:

```bash
#!/usr/bin/env bash
# Build the Loopi dev image (or only the agent) with Buildroot inside Docker.
# Usage: os/build.sh [image|agent|shell]      Output: os/out/
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
TARGET="${1:-image}"
BUILDROOT_REF="${BUILDROOT_REF:-2026.02.x}"
BUILDER_IMAGE=loopi-os-builder
VOLUME=loopi-buildroot   # named volume: case-sensitive and fast on macOS

mkdir -p "$HERE/out"
docker build -q -t "$BUILDER_IMAGE" "$HERE/docker" >/dev/null
docker volume create "$VOLUME" >/dev/null
docker run --rm -t \
  -e BUILDROOT_REF="$BUILDROOT_REF" -e TARGET="$TARGET" \
  -v "$VOLUME:/br" -v "$REPO:/src:ro" -v "$HERE/out:/out" \
  "$BUILDER_IMAGE" /src/os/scripts/container-build.sh
```

`os/scripts/container-build.sh`:

```bash
#!/usr/bin/env bash
# Runs inside the builder container; re-executes itself as the unprivileged "builder" user.
set -euo pipefail
if [ "$(id -u)" = 0 ]; then
  chown builder:builder /br /out
  exec runuser -u builder -- "$0" "$@"
fi
BR=/br/buildroot
OUT=/br/output
SRC=/br/src

# Copy the repo without host build artefacts (Buildroot rsyncs agent/ for the local package).
rsync -a --delete --exclude target --exclude .venv --exclude out --exclude .git /src/ "$SRC/"
[ -d "$BR" ] || git clone --depth 1 --branch "$BUILDROOT_REF" https://gitlab.com/buildroot.org/buildroot.git "$BR"

configure() {
  make -C "$BR" O="$OUT" BR2_EXTERNAL="$SRC/os" raspberrypi0w_defconfig
  "$BR/support/kconfig/merge_config.sh" -m -O "$OUT" "$OUT/.config" \
    "$SRC/os/configs/loopi.fragment" "$SRC/os/configs/dev.fragment"
  make -C "$BR" O="$OUT" olddefconfig
  "$SRC/os/scripts/verify-config.sh" "$OUT/.config" "$SRC/os/configs/loopi.fragment" "$SRC/os/configs/dev.fragment"
}

case "$TARGET" in
  image)
    configure
    make -C "$BR" O="$OUT"
    cp "$OUT/images/sdcard.img" /out/loopi-dev.img
    "$SRC/os/tests/check-image.sh" "$OUT/images"
    ;;
  agent)
    make -C "$BR" O="$OUT" loopi-agent-rebuild
    cp "$OUT/target/usr/bin/loopi-agent" /out/loopi-agent
    ;;
  shell)
    exec bash
    ;;
  *)
    echo "unknown target: $TARGET (use image | agent | shell)" >&2
    exit 2
    ;;
esac
```

`os/scripts/verify-config.sh`:

```bash
#!/usr/bin/env bash
# Fail if a fragment line did not survive into the final .config (renamed symbol, unmet
# dependency or typo). "is not set" lines only fail if the symbol ended up enabled.
set -euo pipefail
config="$1"; shift
missing=0
for fragment in "$@"; do
  while IFS= read -r line; do
    if [[ "$line" =~ ^\#\ (BR2_[A-Z0-9_]+)\ is\ not\ set$ ]]; then
      if grep -q "^${BASH_REMATCH[1]}=y" "$config"; then
        echo "STILL ENABLED: ${BASH_REMATCH[1]} ($(basename "$fragment"))"; missing=1
      fi
    elif [[ "$line" =~ ^BR2_[A-Z0-9_]+= ]]; then
      if ! grep -Fqx -- "$line" "$config"; then
        echo "MISSING: $line ($(basename "$fragment"))"; missing=1
      fi
    fi
  done < "$fragment"
done
[ "$missing" = 0 ] && echo "verify-config: all fragment lines applied"
exit "$missing"
```

Run: `chmod +x os/build.sh os/scripts/*.sh`

- [ ] **Step 4: Write the external tree and fragments**

`os/external.desc`:

```
name: LOOPI
desc: Loopi lifelog device (Raspberry Pi Zero W)
```

`os/external.mk`:

```make
include $(sort $(wildcard $(BR2_EXTERNAL_LOOPI_PATH)/package/*/*.mk))
```

`os/Config.in` (packages are added in Task 3):

```
menu "Loopi"
endmenu
```

`os/configs/loopi.fragment`:

```
# Toolchain built by Buildroot (works on x86_64 and arm64 build hosts)
# BR2_TOOLCHAIN_EXTERNAL is not set
BR2_TOOLCHAIN_BUILDROOT=y
BR2_TOOLCHAIN_BUILDROOT_GLIBC=y
BR2_TOOLCHAIN_BUILDROOT_CXX=y
BR2_KERNEL_HEADERS_6_1=y
# System
BR2_TARGET_GENERIC_HOSTNAME="loopi"
BR2_TARGET_GENERIC_ISSUE="Loopi"
BR2_INIT_SYSTEMD=y
BR2_SYSTEM_DHCP=""
BR2_ROOTFS_OVERLAY="$(BR2_EXTERNAL_LOOPI_PATH)/board/loopi/rootfs-overlay"
BR2_ROOTFS_POST_BUILD_SCRIPT="$(BR2_EXTERNAL_LOOPI_PATH)/board/loopi/post-build.sh"
BR2_ROOTFS_POST_IMAGE_SCRIPT="$(BR2_EXTERNAL_LOOPI_PATH)/board/loopi/post-image.sh"
BR2_ROOTFS_POST_SCRIPT_ARGS=""
# Kernel and firmware (versions come from raspberrypi0w_defconfig)
BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES="$(BR2_EXTERNAL_LOOPI_PATH)/board/loopi/linux.fragment"
BR2_PACKAGE_RPI_FIRMWARE_CONFIG_FILE="$(BR2_EXTERNAL_LOOPI_PATH)/board/loopi/config.txt"
BR2_PACKAGE_RPI_FIRMWARE_INSTALL_DTB_OVERLAYS=y
# Filesystems: read-only squashfs root, host tools for the boot and data images
BR2_TARGET_ROOTFS_SQUASHFS=y
BR2_TARGET_ROOTFS_SQUASHFS4_LZ4=y
# BR2_TARGET_ROOTFS_EXT2 is not set
BR2_PACKAGE_HOST_E2FSPROGS=y
BR2_PACKAGE_HOST_GENIMAGE=y
BR2_PACKAGE_HOST_DOSFSTOOLS=y
BR2_PACKAGE_HOST_MTOOLS=y
```

`os/configs/dev.fragment` (filled in Task 5):

```
# Dev variant additions (Task 5)
```

- [ ] **Step 5: Write the board files**

`os/board/loopi/config.txt`:

```
# Loopi — Raspberry Pi Zero W (kit CSI camera + USB microphone on the OTG port)
start_file=start.elf
fixup_file=fixup.dat
kernel=zImage
disable_overscan=1
# Camera: auto-detect the kit sensor (OV5647 / IMX219 / IMX708)
camera_auto_detect=1
dtoverlay=vc4-kms-v3d
max_framebuffers=1
disable_fw_kms_setup=1
# Onboard analog audio is unused; the microphone is a USB device on the OTG port
dtparam=audio=off
# Serial console on GPIO14/15 for bring-up
enable_uart=1
```

`os/board/loopi/cmdline.txt` (one line):

```
root=/dev/mmcblk0p2 rootfstype=squashfs ro rootwait console=serial0,115200 console=tty1 quiet
```

`os/board/loopi/linux.fragment`:

```
CONFIG_SQUASHFS=y
CONFIG_SQUASHFS_LZ4=y
CONFIG_SQUASHFS_XATTR=y
CONFIG_EXT4_FS=y
CONFIG_VFAT_FS=y
CONFIG_NLS_CODEPAGE_437=y
CONFIG_NLS_ISO8859_1=y
CONFIG_WATCHDOG=y
CONFIG_BCM2835_WDT=y
CONFIG_SND_USB_AUDIO=m
```

`os/board/loopi/genimage.cfg`:

```
image boot.vfat {
	vfat {
		files = {
			"bcm2708-rpi-zero-w.dtb",
			"rpi-firmware/bootcode.bin",
			"rpi-firmware/cmdline.txt",
			"rpi-firmware/config.txt",
			"rpi-firmware/fixup.dat",
			"rpi-firmware/start.elf",
			"rpi-firmware/overlays",
			"zImage",
			"loopi.toml.example"
		}
	}
	size = 64M
}

image sdcard.img {
	hdimage {
	}

	partition boot {
		partition-type = 0xC
		bootable = "true"
		image = "boot.vfat"
	}

	partition rootfs-a {
		partition-type = 0x83
		image = "rootfs.squashfs"
		size = 128M
	}

	partition rootfs-b {
		partition-type = 0x83
		size = 128M
	}

	partition data {
		partition-type = 0x83
		image = "data.ext4"
	}
}
```

`os/board/loopi/loopi.toml.example`:

```toml
# Copy to loopi.toml on this partition and fill in. On first boot the device moves the
# secrets (psk, pair_token) to its data partition and removes them from this file.
[wifi]
ssid = "YourWiFi"
psk = "your-wifi-password"
country = "KR"

[brain]
url = "https://192.168.1.10:8443"
pair_token = "XXXX-XXXX"          # brain web: Devices → "Tạo token ghép cặp"
# ca_fingerprint = "…"            # recommended: copy from the same page

[capture]
video_fps = 2
audio_enabled = true

[schedule]
timezone = "Asia/Seoul"
quiet_hours = []                  # e.g. ["00:00-07:00"]
```

`os/board/loopi/post-build.sh` (extended in Task 2):

```sh
#!/bin/sh
# Runs after the target tree is assembled, before the root filesystem image is made.
set -eu
```

`os/board/loopi/post-image.sh`:

```sh
#!/bin/sh
# Assemble the SD card: boot (FAT), rootfs-A (squashfs), rootfs-B (empty), data (ext4).
set -eu
BOARD_DIR="$(cd "$(dirname "$0")" && pwd)"

cp "${BOARD_DIR}/cmdline.txt" "${BINARIES_DIR}/rpi-firmware/cmdline.txt"
cp "${BOARD_DIR}/loopi.toml.example" "${BINARIES_DIR}/loopi.toml.example"

DATA_IMG="${BINARIES_DIR}/data.ext4"
EMPTY_DIR="$(mktemp -d)"
rm -f "${DATA_IMG}"
truncate -s 64M "${DATA_IMG}"
"${HOST_DIR}/sbin/mkfs.ext4" -q -F -L data -d "${EMPTY_DIR}" "${DATA_IMG}"
rmdir "${EMPTY_DIR}"

support/scripts/genimage.sh -c "${BOARD_DIR}/genimage.cfg"
```

Run: `chmod +x os/board/loopi/post-build.sh os/board/loopi/post-image.sh && mkdir -p os/board/loopi/rootfs-overlay/etc && touch os/board/loopi/rootfs-overlay/etc/.keep`

- [ ] **Step 6: Build and run the checks**

Run: `./os/build.sh image`
Expected: the first build takes 1–3 h (toolchain, kernel, systemd). It prints `verify-config: all fragment lines applied`, every Task 1 check is `[ OK ]`, and `os/out/loopi-dev.img` exists. If `verify-config` reports `MISSING`, run `./os/build.sh shell`, then `make -C /br/buildroot O=/br/output menuconfig`, search the symbol with `/`, fix the fragment and rebuild.

- [ ] **Step 7: Boot on the Pi (serial console)**

Flash: `sudo dd if=os/out/loopi-dev.img of=/dev/sdX bs=4M conv=fsync status=progress` (or Raspberry Pi Imager → "Use custom"). Connect a 3.3 V USB-serial adapter to GPIO14/15, run `screen /dev/ttyUSB0 115200`, power the Pi.
Expected: `loopi login:` within ~60 s; `root` logs in without a password on the serial console; `mount | grep ' / '` shows `squashfs (ro`.

- [ ] **Step 8: Commit**

```bash
git add os
git commit -m "feat(os): Buildroot external tree, builder container and bootable base image"
```

---

### Task 2: Read-only root, tmpfs, data partition grow, watchdog

**Files:**
- Create: `os/board/loopi/rootfs-overlay/etc/fstab`, `.../etc/systemd/journald.conf.d/10-loopi.conf`, `.../etc/systemd/system.conf.d/10-watchdog.conf`, `.../etc/systemd/system/loopi-growdata.service`, `.../usr/libexec/loopi/grow-data.sh`
- Modify: `os/board/loopi/post-build.sh`, `os/configs/loopi.fragment`, `os/tests/check-image.sh`

**Interfaces:**
- Produces: `/data` mounted ext4 from p4; `/var`, `/tmp` tmpfs; `loopi-growdata.service` (oneshot, `ConditionPathExists=!/data/.grown`); `post-build.sh` helper `enable_unit <name>` used by later tasks.

- [ ] **Step 1: Add the failing checks to `os/tests/check-image.sh` (before `exit $fail`)**

```bash
# --- Task 2: read-only root, data partition, watchdog
check "fstab mounts / read-only squashfs" rootfs_file_contains etc/fstab '^/dev/root +/ +squashfs +ro'
check "fstab mounts /data from p4 (ext4)" rootfs_file_contains etc/fstab '^/dev/mmcblk0p4 +/data +ext4'
check "fstab mounts /var as tmpfs" rootfs_file_contains etc/fstab '^tmpfs +/var +tmpfs'
check "journald is volatile" rootfs_file_contains etc/systemd/journald.conf.d/10-loopi.conf '^Storage=volatile'
check "hardware watchdog enabled" rootfs_file_contains etc/systemd/system.conf.d/10-watchdog.conf '^RuntimeWatchdogSec=15s'
check "data partition grows on first boot" rootfs_has /etc/systemd/system/multi-user.target.wants/loopi-growdata.service
check "grow tools present" rootfs_has /usr/sbin/resize2fs
check "data image is labelled 'data'" bash -c "dumpe2fs -h '$IMAGES/data.ext4' 2>/dev/null | grep -q 'volume name: *data'"
```

Run: `./os/build.sh image`
Expected: the new checks `[FAIL]`; the script exits 1.

- [ ] **Step 2: Add packages to `os/configs/loopi.fragment`**

```
# Read-only root support and first-boot data partition grow
BR2_INIT_SYSTEMD_VAR_FACTORY=y
BR2_PACKAGE_UTIL_LINUX=y
BR2_PACKAGE_UTIL_LINUX_FDISKS=y
BR2_PACKAGE_UTIL_LINUX_PARTX=y
BR2_PACKAGE_E2FSPROGS=y
BR2_PACKAGE_E2FSPROGS_RESIZE2FS=y
```

- [ ] **Step 3: Write the overlay files**

`rootfs-overlay/etc/fstab`:

```
# <device>      <mount> <type>    <options>                           <dump> <pass>
/dev/root       /       squashfs  ro                                  0      0
/dev/mmcblk0p1  /boot   vfat      defaults,noatime,nofail,umask=0022  0      0
/dev/mmcblk0p4  /data   ext4      defaults,noatime,commit=30          0      2
tmpfs           /tmp    tmpfs     mode=1777,nosuid,nodev              0      0
tmpfs           /var    tmpfs     mode=0755,nosuid,nodev              0      0
```

`rootfs-overlay/etc/systemd/journald.conf.d/10-loopi.conf`:

```ini
[Journal]
Storage=volatile
RuntimeMaxUse=16M
```

`rootfs-overlay/etc/systemd/system.conf.d/10-watchdog.conf`:

```ini
[Manager]
RuntimeWatchdogSec=15s
```

`rootfs-overlay/usr/libexec/loopi/grow-data.sh`:

```sh
#!/bin/sh
# Grow the data partition (p4) to the end of the SD card. Safe to re-run: every step is
# idempotent and the marker is written only after resize2fs succeeded.
set -eu
DISK=/dev/mmcblk0
PART=/dev/mmcblk0p4
MARKER=/data/.grown
[ -e "$MARKER" ] && exit 0
echo ", +" | sfdisk --no-reread --no-tell-kernel -N 4 "$DISK"
partx -u -n 4 "$DISK"
resize2fs "$PART"
touch "$MARKER"
```

`rootfs-overlay/etc/systemd/system/loopi-growdata.service`:

```ini
[Unit]
Description=Grow the Loopi data partition to fill the SD card
RequiresMountsFor=/data
ConditionPathExists=!/data/.grown
Before=loopi-agent.service

[Service]
Type=oneshot
ExecStart=/usr/libexec/loopi/grow-data.sh

[Install]
WantedBy=multi-user.target
```

Run: `chmod +x os/board/loopi/rootfs-overlay/usr/libexec/loopi/grow-data.sh && rm os/board/loopi/rootfs-overlay/etc/.keep`

- [ ] **Step 4: Enable units from `post-build.sh`**

Replace `os/board/loopi/post-build.sh` with:

```sh
#!/bin/sh
# Runs after the target tree is assembled, before the root filesystem image is made.
# The root filesystem is read-only, so units are enabled here rather than at runtime.
set -eu
WANTS="${TARGET_DIR}/etc/systemd/system/multi-user.target.wants"
mkdir -p "$WANTS"

enable_unit() {
	ln -sf "../$1" "$WANTS/$1"
}

enable_unit loopi-growdata.service
```

- [ ] **Step 5: Rebuild and run the checks**

Run: `./os/build.sh image`
Expected: all Task 1 and Task 2 checks `[ OK ]`.

- [ ] **Step 6: Verify on the device**

Flash and boot (serial console), then run:
```sh
mount | grep -E ' on (/|/data|/var|/tmp) '
df -h /data
systemctl --failed
ls /dev/watchdog0
```
Expected: `/` is `squashfs (ro`; `/var` and `/tmp` are `tmpfs`; `/data` is `ext4 (rw` sized close to the card minus ~260 MiB; `systemctl --failed` shows `0 loaded units listed`; `/dev/watchdog0` exists.

- [ ] **Step 7: Re-run the grow script (idempotency, Review Focus 1)**

Run on the device: `rm /data/.grown && /usr/libexec/loopi/grow-data.sh && ls -l /data/.grown && df -h /data`
Expected: `resize2fs` reports "Nothing to do!", the marker is recreated and the `df` size is unchanged. Then flash a fresh card, pull the power within ~20 s of the first power-on, power on again, and confirm `/data` still ends up full-size with `/data/.grown` present.

- [ ] **Step 8: Commit**

```bash
git add os
git commit -m "feat(os): read-only squashfs root, tmpfs /var, data partition grow and watchdog"
```

---

### Task 3: Agent package, WiFi, first-boot provisioning and the agent service

**Files:**
- Create: `os/package/loopi-agent/Config.in`, `os/package/loopi-agent/loopi-agent.mk`, `os/board/loopi/rootfs-overlay/etc/systemd/network/20-wlan0.network`, `.../etc/systemd/system/{loopi-provision,loopi-wifi,loopi-agent}.service`, `.../usr/libexec/loopi/run-agent`, `.../etc/loopi-dev`
- Modify: `os/Config.in`, `os/configs/loopi.fragment`, `os/board/loopi/post-build.sh`, `os/tests/check-image.sh`

**Interfaces:**
- Consumes: `loopi-agent provision --boot /boot/loopi.toml --data /data` and `loopi-agent run` (P1b Task 12); `/data/config/wpa_supplicant-wlan0.conf` written by provisioning; the P1a brain on the LAN.
- Produces: `/usr/bin/loopi-agent` (ARMv6); units `loopi-provision.service`, `loopi-wifi.service`, `loopi-agent.service`; `/usr/libexec/loopi/run-agent` (prefers `/data/dev/loopi-agent` on dev images).

- [ ] **Step 1: Add the failing checks to `os/tests/check-image.sh` (before `exit $fail`)**

```bash
# --- Task 3: agent, networking, provisioning
check "agent binary is 32-bit ARM" bash -c "unsquashfs -cat '$IMAGES/rootfs.squashfs' usr/bin/loopi-agent > /tmp/loopi-agent.bin && file /tmp/loopi-agent.bin | grep -q 'ARM, EABI5'"
check "agent service enabled" rootfs_has /etc/systemd/system/multi-user.target.wants/loopi-agent.service
check "provisioning enabled" rootfs_has /etc/systemd/system/multi-user.target.wants/loopi-provision.service
check "wifi enabled" rootfs_has /etc/systemd/system/multi-user.target.wants/loopi-wifi.service
check "agent has a systemd watchdog" rootfs_file_contains etc/systemd/system/loopi-agent.service '^WatchdogSec=30'
check "recorders die with the agent" rootfs_file_contains etc/systemd/system/loopi-agent.service '^KillMode=control-group'
check "wlan0 uses DHCP" rootfs_file_contains etc/systemd/network/20-wlan0.network '^DHCP=yes'
check "WiFi firmware present" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' | grep -q brcmfmac43430"
check "BlueZ present" rootfs_has /usr/bin/bluetoothctl
check "timesyncd present" rootfs_has /usr/lib/systemd/systemd-timesyncd
```

Run: `./os/build.sh image`
Expected: the Task 3 checks `[FAIL]`.

- [ ] **Step 2: Write the agent package**

`os/package/loopi-agent/Config.in`:

```
config BR2_PACKAGE_LOOPI_AGENT
	bool "loopi-agent"
	depends on BR2_PACKAGE_HOST_RUSTC_TARGET_ARCH_SUPPORTS
	select BR2_PACKAGE_HOST_RUSTC
	select BR2_PACKAGE_ALSA_LIB
	select BR2_PACKAGE_OPUS
	help
	  Loopi capture agent: records video and audio and uploads
	  them to the Loopi brain.
```

`os/package/loopi-agent/loopi-agent.mk`:

```make
################################################################################
#
# loopi-agent
#
################################################################################

LOOPI_AGENT_VERSION = 0.1.0
LOOPI_AGENT_SITE = $(BR2_EXTERNAL_LOOPI_PATH)/../agent
LOOPI_AGENT_SITE_METHOD = local
LOOPI_AGENT_LICENSE = Proprietary
LOOPI_AGENT_DEPENDENCIES = host-rustc host-pkgconf alsa-lib opus

# Same environment as Buildroot's cargo-package infrastructure (cross linker, target triple,
# pkg-config sysroot). Crates are fetched from crates.io; Cargo.lock pins every version.
define LOOPI_AGENT_BUILD_CMDS
	cd $(@D) && $(TARGET_MAKE_ENV) $(TARGET_CONFIGURE_OPTS) $(PKG_CARGO_ENV) \
		PKG_CONFIG_ALLOW_CROSS=1 \
		cargo build --release --locked --bin loopi-agent
endef

define LOOPI_AGENT_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/target/$(RUSTC_TARGET_NAME)/release/loopi-agent \
		$(TARGET_DIR)/usr/bin/loopi-agent
endef

$(eval $(generic-package))
```

Replace `os/Config.in` with:

```
menu "Loopi"
	source "$BR2_EXTERNAL_LOOPI_PATH/package/loopi-agent/Config.in"
endmenu
```

- [ ] **Step 3: Add networking and the agent to `os/configs/loopi.fragment`**

```
# Networking: WiFi via wpa_supplicant + systemd-networkd, NTP, BLE stack for later features
BR2_PACKAGE_SYSTEMD_NETWORKD=y
BR2_PACKAGE_SYSTEMD_RESOLVED=y
BR2_PACKAGE_SYSTEMD_TIMESYNCD=y
BR2_PACKAGE_WPA_SUPPLICANT=y
BR2_PACKAGE_BRCMFMAC_SDIO_FIRMWARE_RPI=y
BR2_PACKAGE_BRCMFMAC_SDIO_FIRMWARE_RPI_WIFI=y
BR2_PACKAGE_BRCMFMAC_SDIO_FIRMWARE_RPI_BT=y
BR2_PACKAGE_BLUEZ5_UTILS=y
BR2_PACKAGE_CA_CERTIFICATES=y
# Agent and its native libraries
BR2_PACKAGE_HOST_RUSTC=y
BR2_PACKAGE_HOST_RUST_BIN=y
BR2_PACKAGE_ALSA_LIB=y
BR2_PACKAGE_OPUS=y
BR2_PACKAGE_LOOPI_AGENT=y
```

- [ ] **Step 4: Write the overlay files**

`rootfs-overlay/etc/systemd/network/20-wlan0.network`:

```ini
[Match]
Name=wlan0

[Network]
DHCP=yes
```

`rootfs-overlay/etc/systemd/system/loopi-provision.service`:

```ini
[Unit]
Description=Import Loopi settings from the boot partition
RequiresMountsFor=/data /boot
ConditionPathExists=/boot/loopi.toml
Before=loopi-wifi.service loopi-agent.service

[Service]
Type=oneshot
ExecStart=/usr/bin/loopi-agent provision --boot /boot/loopi.toml --data /data

[Install]
WantedBy=multi-user.target
```

`rootfs-overlay/etc/systemd/system/loopi-wifi.service`:

```ini
[Unit]
Description=WiFi for wlan0 (wpa_supplicant, config on /data)
After=loopi-provision.service sys-subsystem-net-devices-wlan0.device
BindsTo=sys-subsystem-net-devices-wlan0.device
ConditionPathExists=/data/config/wpa_supplicant-wlan0.conf
Before=network.target
Wants=network.target

[Service]
ExecStart=/usr/sbin/wpa_supplicant -i wlan0 -c /data/config/wpa_supplicant-wlan0.conf
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`rootfs-overlay/etc/systemd/system/loopi-agent.service`:

```ini
[Unit]
Description=Loopi capture agent
After=loopi-provision.service loopi-growdata.service network-online.target
Wants=network-online.target
RequiresMountsFor=/data

[Service]
Type=notify
NotifyAccess=main
ExecStart=/usr/libexec/loopi/run-agent
Restart=always
RestartSec=2
WatchdogSec=30
KillMode=control-group
TimeoutStopSec=15
Environment=RUST_LOG=info

[Install]
WantedBy=multi-user.target
```

`rootfs-overlay/usr/libexec/loopi/run-agent`:

```sh
#!/bin/sh
# Dev images run /data/dev/loopi-agent when present (deployed with os/scripts/deploy-agent.sh);
# otherwise the packaged binary. exec keeps the agent as the service's main PID (sd_notify).
if [ -f /etc/loopi-dev ] && [ -x /data/dev/loopi-agent ]; then
	exec /data/dev/loopi-agent run
fi
exec /usr/bin/loopi-agent run
```

`rootfs-overlay/etc/loopi-dev`:

```
This is a Loopi development image.
```

Run: `chmod +x os/board/loopi/rootfs-overlay/usr/libexec/loopi/run-agent`

- [ ] **Step 5: Enable the units in `post-build.sh`**

Append to `os/board/loopi/post-build.sh`:

```sh
enable_unit loopi-provision.service
enable_unit loopi-wifi.service
enable_unit loopi-agent.service
```

- [ ] **Step 6: Rebuild and run the checks**

Run: `./os/build.sh image`
Expected: all checks up to Task 3 `[ OK ]`. The first build of `loopi-agent` downloads crates and the Rust toolchain (~15 min). If linking fails with a missing `libasound`/`libopus`, confirm `LOOPI_AGENT_DEPENDENCIES` and rebuild with `./os/build.sh shell` → `make -C /br/buildroot O=/br/output loopi-agent-dirclean all`.

- [ ] **Step 7: First boot against the brain**

On the brain (P1a): Devices page → "Tạo token ghép cặp". Mount the SD card's boot partition on your computer, copy `loopi.toml.example` to `loopi.toml` and fill in `wifi`, `brain.url` (the brain's LAN IP), `pair_token` and `ca_fingerprint`. Boot the Pi and on the serial console run:
```sh
journalctl -b -u loopi-provision -u loopi-wifi -u loopi-agent --no-pager | tail -40
networkctl status wlan0 | head -5
timedatectl show -p NTPSynchronized
cat /boot/loopi.toml
```
Expected: provisioning prints `Imported`; `wlan0` is `routable` with an IP; `NTPSynchronized=yes`; the agent logs `paired`; `/boot/loopi.toml` no longer contains `psk` or `pair_token`. On the brain's Devices page the device is "trực tuyến". (Camera and mic packages arrive in Task 4, so the agent reports camera failures and `mic_missing` for now.)

- [ ] **Step 8: Second boot does not re-import (Review Focus 2)**

Run on the device: `reboot`, then after it is back: `journalctl -b -u loopi-provision --no-pager | tail -3 && networkctl status wlan0 | head -3`
Expected: `AlreadyImported`; WiFi `routable`; the device is online in the brain again without a new pair token.

- [ ] **Step 9: Commit**

```bash
git add os
git commit -m "feat(os): loopi-agent package, WiFi provisioning from the boot partition and agent service"
```

---

### Task 4: Kit CSI camera and USB microphone

**Files:**
- Modify: `os/configs/loopi.fragment`, `os/configs/dev.fragment`, `os/tests/check-image.sh`

**Interfaces:**
- Consumes: `config.txt` (`camera_auto_detect=1`, `vc4-kms-v3d`), kernel module `snd-usb-audio` (Task 1 fragment), agent `selftest` (P1b Task 12).
- Produces: `/usr/bin/rpicam-vid` and libcamera's Raspberry Pi IPA in the image; `arecord` on dev images.

- [ ] **Step 1: Add the failing checks to `os/tests/check-image.sh` (before `exit $fail`)**

```bash
# --- Task 4: camera and USB microphone
check "rpicam-vid present" rootfs_has /usr/bin/rpicam-vid
check "libcamera Raspberry Pi IPA present" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' | grep -q 'ipa_rpi_vc4'"
check "USB audio driver present" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' | grep -q 'snd-usb-audio.ko'"
check "camera auto-detect enabled" grep -qx 'camera_auto_detect=1' "$IMAGES/rpi-firmware/config.txt"
check "onboard audio disabled" grep -qx 'dtparam=audio=off' "$IMAGES/rpi-firmware/config.txt"
check "KMS overlay shipped" boot_has overlays/vc4-kms-v3d.dtbo
check "OV5647 overlay shipped" boot_has overlays/ov5647.dtbo
check "arecord present (dev)" rootfs_has /usr/bin/arecord
```

Run: `./os/build.sh image`
Expected: the camera and `arecord` checks `[FAIL]`.

- [ ] **Step 2: Add camera packages to `os/configs/loopi.fragment`**

```
# Camera: libcamera with the Raspberry Pi (VC4) pipeline + rpicam-apps (Buildroot name libcamera-apps)
BR2_PACKAGE_LIBCAMERA=y
BR2_PACKAGE_LIBCAMERA_PIPELINE_RPI_VC4=y
BR2_PACKAGE_LIBCAMERA_APPS=y
```

- [ ] **Step 3: Add audio tools to `os/configs/dev.fragment`**

Replace the file with:

```
# Dev variant: audio tools for bring-up
BR2_PACKAGE_ALSA_UTILS=y
BR2_PACKAGE_ALSA_UTILS_APLAY=y
```

- [ ] **Step 4: Rebuild and run the checks**

Run: `./os/build.sh image`
Expected: all checks up to Task 4 `[ OK ]`. If `libcamera-apps` fails to build against Buildroot's libcamera (risk R1), use `./os/build.sh shell` → `grep -E '^(LIBCAMERA|LIBCAMERA_APPS)_VERSION' /br/buildroot/package/libcamera*/*.mk` and pin a matching rpicam-apps release in an external package `os/package/rpicam-apps/` (copy Buildroot's `libcamera-apps.mk`, change `*_VERSION`/`*_SITE`, disable the upstream one with `# BR2_PACKAGE_LIBCAMERA_APPS is not set`). If only the IPA check fails, list `/usr/lib/libcamera` in the squashfs and update the pattern to the real file name.

- [ ] **Step 5: Verify the camera on the device**

Flash, boot, then on the device:
```sh
rpicam-vid --list-cameras
rpicam-vid -t 6000 -n --codec h264 --inline --framerate 2 --width 1920 --height 1080 -o /tmp/test.h264
ls -l /tmp/test.h264
```
Expected: the kit sensor is listed (e.g. `0 : ov5647 [2592x1944 …]` or `imx219`); `/tmp/test.h264` is non-empty. Copy it to your computer (`scp` after Task 5, or via the serial console's file transfer) and check with `ffprobe -f h264 -framerate 2 test.h264` → about 12 frames.

- [ ] **Step 6: Verify the USB microphone**

Plug the USB mic into the OTG port (via adapter if needed), then:
```sh
arecord -l
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 3 /tmp/mic.wav && ls -l /tmp/mic.wav
loopi-agent selftest
```
Expected: `arecord -l` lists a `USB Audio` card; `/tmp/mic.wav` is about 96 KB; `selftest` shows `[ OK ]` for camera and microphone (level well above -120 dBFS while you speak).

- [ ] **Step 7: End-to-end recording to the brain**

Run: `systemctl restart loopi-agent && sleep 150`, then open the brain timeline for today.
Expected: within 2 minutes there are video segments that play in the browser (after remux) and audio segments that play; the LED is solid; the Devices page shows `recording_video` and `recording_audio` true and `camera_failures` 0.

- [ ] **Step 8: Camera unplugged at boot (Review Focus 5)**

Power off, disconnect the camera ribbon cable, power on. After ~2 minutes:
```sh
systemctl is-active loopi-agent
journalctl -b -u loopi-agent --no-pager | grep -c "rpicam-vid exited"
loopi-agent selftest | grep camera
```
Expected: the agent is `active`; at least 5 "rpicam-vid exited" lines with growing gaps (≤ 60 s); selftest camera `[FAIL]`; the LED fast-blinks when not recording audio, and is solid while the mic records; audio segments keep arriving on the brain; the Devices page shows `camera_failures ≥ 5`. Power off and reconnect the camera afterwards.

- [ ] **Step 9: Commit**

```bash
git add os
git commit -m "feat(os): libcamera + rpicam-apps for the kit camera and USB microphone support"
```

---

### Task 5: Dev access — key-only SSH, persistent host keys, agent hot-deploy

**Files:**
- Create: `os/board/loopi/rootfs-overlay/etc/tmpfiles.d/loopi.conf`, `.../etc/systemd/system/loopi-devkeys.service`, `os/scripts/deploy-agent.sh`, `os/README.md`
- Modify: `os/configs/dev.fragment`, `os/board/loopi/post-build.sh`, `os/tests/check-image.sh`

**Interfaces:**
- Consumes: `/etc/loopi-dev` marker and `run-agent` (Task 3), `os/build.sh agent` (Task 1).
- Produces: dropbear on port 22 (keys only), `/etc/dropbear → /data/dev/dropbear`, `/root/.ssh → /data/dev/ssh`, `loopi-devkeys.service` (copies `boot/loopi-authorized_keys`), `os/scripts/deploy-agent.sh <device-ip>`.

- [ ] **Step 1: Add the failing checks to `os/tests/check-image.sh` (before `exit $fail`)**

```bash
# --- Task 5: dev access (key-only SSH, rsync)
check "dropbear enabled" rootfs_has /etc/systemd/system/multi-user.target.wants/dropbear.service
check "SSH host keys persist on /data" bash -c "unsquashfs -ll '$IMAGES/rootfs.squashfs' | grep -q 'etc/dropbear -> /data/dev/dropbear'"
check "authorized_keys live on /data" bash -c "unsquashfs -ll '$IMAGES/rootfs.squashfs' | grep -q 'root/.ssh -> /data/dev/ssh'"
check "SSH keys imported from the boot partition" rootfs_has /etc/systemd/system/multi-user.target.wants/loopi-devkeys.service
check "rsync present" rootfs_has /usr/bin/rsync
check "dev marker present" rootfs_has /etc/loopi-dev
```

Run: `./os/build.sh image`
Expected: the Task 5 checks `[FAIL]`.

- [ ] **Step 2: Extend `os/configs/dev.fragment`**

Append:

```
# Dev variant: SSH (key-only via /data/dev/ssh), file sync, tracing
BR2_PACKAGE_DROPBEAR=y
BR2_PACKAGE_RSYNC=y
BR2_PACKAGE_STRACE=y
```

- [ ] **Step 3: Write the overlay files**

`rootfs-overlay/etc/tmpfiles.d/loopi.conf`:

```
d /data/dev          0755 root root -
d /data/dev/ssh      0700 root root -
d /data/dev/dropbear 0700 root root -
```

`rootfs-overlay/etc/systemd/system/loopi-devkeys.service`:

```ini
[Unit]
Description=Install SSH authorized_keys from the boot partition (dev image)
RequiresMountsFor=/data /boot
ConditionPathExists=/boot/loopi-authorized_keys
After=systemd-tmpfiles-setup.service
Before=dropbear.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'install -d -m 0700 /data/dev/ssh && install -m 0600 /boot/loopi-authorized_keys /data/dev/ssh/authorized_keys'

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Persist SSH state on `/data` from `post-build.sh`**

Append to `os/board/loopi/post-build.sh`:

```sh
# Dev access: the root filesystem is read-only, so SSH host keys and authorized_keys live on /data.
if [ -e "${TARGET_DIR}/etc/loopi-dev" ]; then
	rm -rf "${TARGET_DIR}/etc/dropbear" "${TARGET_DIR}/root/.ssh"
	ln -s /data/dev/dropbear "${TARGET_DIR}/etc/dropbear"
	mkdir -p "${TARGET_DIR}/root"
	ln -s /data/dev/ssh "${TARGET_DIR}/root/.ssh"
	ln -sf /usr/lib/systemd/system/dropbear.service "$WANTS/dropbear.service"
	enable_unit loopi-devkeys.service
fi
```

- [ ] **Step 5: Write `os/scripts/deploy-agent.sh` and `os/README.md`**

`os/scripts/deploy-agent.sh`:

```bash
#!/usr/bin/env bash
# Rebuild the agent with Buildroot and hot-swap it on a dev device (no reflash).
# Usage: os/scripts/deploy-agent.sh <device-ip>
set -euo pipefail
HOST="${1:?usage: deploy-agent.sh <device-ip>}"
OS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
"$OS_DIR/build.sh" agent
rsync -z "$OS_DIR/out/loopi-agent" "root@$HOST:/data/dev/loopi-agent.new"
ssh "root@$HOST" 'chmod 755 /data/dev/loopi-agent.new \
  && mv /data/dev/loopi-agent.new /data/dev/loopi-agent \
  && systemctl restart loopi-agent && systemctl is-active loopi-agent'
```

Run: `chmod +x os/scripts/deploy-agent.sh`

`os/README.md`:

```markdown
# Loopi OS (Buildroot, Raspberry Pi Zero W)

## Build
`./os/build.sh image` → `os/out/loopi-dev.img` (needs Docker; first build 1–3 h, later builds minutes).

## Flash and configure
1. `sudo dd if=os/out/loopi-dev.img of=/dev/sdX bs=4M conv=fsync status=progress`
   (or Raspberry Pi Imager → "Use custom").
2. On the `boot` partition: copy `loopi.toml.example` to `loopi.toml` and fill in WiFi, the brain URL,
   `pair_token` and `ca_fingerprint` (brain web → Thiết bị → "Tạo token ghép cặp").
3. Dev images: put your SSH public key in `boot/loopi-authorized_keys`.
4. Insert the card, plug the USB mic into the OTG port (`USB`, not `PWR`), power on.

On first boot the device grows the data partition, imports `loopi.toml` (secrets are then removed
from the boot partition), joins WiFi, pairs with the brain and starts recording (LED solid).

## Develop the agent without reflashing
`os/scripts/deploy-agent.sh <device-ip>` rebuilds the ARMv6 binary and runs it from `/data/dev/`.
Remove `/data/dev/loopi-agent` on the device to go back to the packaged binary.

## Useful commands on the device
`loopi-agent selftest` · `journalctl -u loopi-agent -f` · `systemctl --failed` · `rpicam-vid --list-cameras` · `arecord -l`
```

- [ ] **Step 6: Rebuild, then verify SSH and read-only robustness on the device (Review Focus 3)**

Run: `./os/build.sh image` → all checks `[ OK ]`. Flash, put `~/.ssh/id_ed25519.pub` into `boot/loopi-authorized_keys`, boot, then from your computer:
```bash
ssh root@<device-ip> 'systemctl --failed --no-legend | wc -l; uptime'
ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password root@<device-ip> true; echo "password login exit=$?"
ssh-keyscan -t ed25519,ecdsa <device-ip> 2>/dev/null | ssh-keygen -lf - > /tmp/hostkey-1
ssh root@<device-ip> reboot; sleep 90
ssh-keyscan -t ed25519,ecdsa <device-ip> 2>/dev/null | ssh-keygen -lf - > /tmp/hostkey-2
diff /tmp/hostkey-1 /tmp/hostkey-2 && echo "host key stable"
```
Expected: `0` failed units; password login fails (exit ≠ 0); `host key stable`.

- [ ] **Step 7: Hot-deploy the agent**

Run: `./os/scripts/deploy-agent.sh <device-ip>` then `ssh root@<device-ip> 'systemctl status loopi-agent --no-pager | grep -m1 /data/dev'`
Expected: `active`; the status shows the process `/data/dev/loopi-agent run`.

- [ ] **Step 8: Commit**

```bash
git add os
git commit -m "feat(os): key-only SSH with persistent host keys and agent hot-deploy for dev images"
```

---

### Task 6: Phase 1 acceptance on real hardware

**Files:**
- Create: `docs/measurements/2026-10-p1-acceptance.md`

**Interfaces:**
- Consumes: the running brain (P1a, `deploy/`), a device flashed with the Task 5 image, the agent (P1b).
- Produces: a filled-in acceptance record with measurements and a go/no-go for Phase 2.

- [ ] **Step 1: Create the acceptance record**

`docs/measurements/2026-10-p1-acceptance.md`:

```markdown
# Phase 1 acceptance — Loopi dev image

- Date: <YYYY-MM-DD> · Image commit: <git rev-parse --short HEAD> · Camera sensor: <from selftest>
- USB mic: <model> · SD card: <size/model> · Brain host: <CPU/RAM>

| # | Spec §14 P1 criterion | Result | Evidence |
|---|---|---|---|
| 1 | Flash + `loopi.toml` → pairs and first segment on the timeline ≤ 2 min | | |
| 2 | 24 h without agent crash (`NRestarts=0`) | | |
| 3 | Brain unreachable 1 h → queue drains afterwards, no gaps | | |
| 4 | 10 power pulls → boots normally, `systemctl --failed` empty | | |
| 5 | Agent SIGKILL → recording stops and LED off | | |

| Measurement | Value |
|---|---|
| CPU % (agent + rpicam-vid), 1-min average | |
| RAM: agent RSS / rpicam-vid RSS / `free -m` available | |
| dwc_otg interrupts per second (risk R8) | |
| SoC temperature after 1 h | |
| Video bitrate (kbps, hourly average) | |
| Storage per day: video / audio | |

Go / no-go for Phase 2: <decision and follow-ups>
```

- [ ] **Step 2: Criterion 1 — time to first segment**

Start a stopwatch, flash a fresh card with `loopi.toml` + `loopi-authorized_keys`, power on. Stop when the first video segment appears on the brain timeline.
Expected: ≤ 2 min after the LED turns solid (first boot also grows `/data`; note the total time too). Record both times.

- [ ] **Step 3: Criterion 2 — 24-hour soak**

After 24 h run: `ssh root@<ip> 'systemctl show loopi-agent -p NRestarts; journalctl -u loopi-agent -p warning --since -24h --no-pager | wc -l; loopi-agent selftest'`
Expected: `NRestarts=0`; warnings explained (e.g. none, or transient WiFi); selftest all `[ OK ]` except intentionally unplugged parts.

- [ ] **Step 4: Criterion 3 — brain outage of 1 hour**

On the brain host: `cd deploy && docker compose stop proxy`; wait 60 min; `docker compose start proxy`. Watch the Devices page `queue_depth` rise and return to 0. Then check for gaps:
```bash
docker compose exec -T db psql -U loopi -d loopi -c "
WITH v AS (
  SELECT started_at, lag(ended_at) OVER (ORDER BY started_at) AS prev_end
  FROM item WHERE kind = 'video' AND started_at >= now() - interval '3 hours')
SELECT prev_end, started_at, started_at - prev_end AS gap
FROM v WHERE started_at - prev_end > interval '5 seconds';"
```
Expected: zero rows; `dropped_total` unchanged on the Devices page; the queue drained within ~15 min (record the time).

- [ ] **Step 5: Criterion 4 — ten power pulls**

Repeat 10×: let it record ≥ 2 min, pull the power, wait 5 s, power on, wait for the LED to turn solid. Then:
```bash
ssh root@<ip> 'systemctl --failed --no-legend | wc -l; journalctl -b -u loopi-agent --no-pager | grep "queue ready"'
```
Expected: `0` failed units on every boot; `queue ready` shows `recovered ≥ 0` and `dropped` 0 or 1 per pull; the brain keeps receiving segments; no `.tmp` files accumulate (`ssh root@<ip> 'find /data/queue -name "*.tmp" | wc -l'` → 0).

- [ ] **Step 6: Criterion 5 — kill the agent (Review Focus 4)**

Run: `ssh root@<ip> 'kill -9 $(pidof loopi-agent); sleep 1; pidof rpicam-vid >/dev/null && echo STILL-RECORDING || echo stopped'` while watching the LED.
Expected: `stopped`; the LED goes off within ~1 s and returns to solid after systemd restarts the agent (~2–5 s).

- [ ] **Step 7: Measurements**

On the device (while recording):
```bash
ssh root@<ip> 'top -b -n 1 | head -15; free -m; grep VmRSS /proc/$(pidof loopi-agent)/status /proc/$(pidof rpicam-vid)/status; cat /sys/class/thermal/thermal_zone0/temp'
ssh root@<ip> 'a=$(grep dwc_otg /proc/interrupts | awk "{print \$2}"); sleep 10; b=$(grep dwc_otg /proc/interrupts | awk "{print \$2}"); echo $(( (b - a) / 10 )) irq/s'
```
On the brain:
```bash
docker compose exec -T db psql -U loopi -d loopi -c "
SELECT date_trunc('hour', started_at) AS hour, round(sum(size_bytes) * 8 / 3600.0 / 1000) AS kbps
FROM item WHERE kind = 'video' GROUP BY 1 ORDER BY 1 DESC LIMIT 6;"
docker compose exec -T db psql -U loopi -d loopi -c "
SELECT kind, pg_size_pretty(sum(size_bytes)) FROM item
WHERE started_at >= now() - interval '1 day' GROUP BY kind;"
```
Fill in every row of the measurements table. Compare with the spec estimates (0.5–1.5 Mbps, 5–10 GB/day video). If CPU is above ~80 % or audio has gaps, apply the R2/R8 mitigations (lower `video_fps`/resolution, a mic that supports 16 kHz natively) and re-measure.

- [ ] **Step 8: Decide and commit**

Write the go/no-go line (all five criteria pass → go to Phase 2; otherwise list the fixes as follow-up tasks).

```bash
git add docs/measurements/2026-10-p1-acceptance.md
git commit -m "docs: Phase 1 acceptance results and measurements"
```
