#!/usr/bin/env bash
# Structural checks on a built Loopi image. Usage: check-image.sh <buildroot images dir>
set -uo pipefail
IMAGES="${1:?usage: check-image.sh <images dir>}"
fail=0

check() {  # check <description> <command...>
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "[ OK ] $name"; else echo "[FAIL] $name"; fail=1; fi
}
# No "grep -q" below: it exits at the first match, unsquashfs then dies of SIGPIPE and pipefail
# turns a present file into a failure. Plain grep drains the pipe; check() discards the output.
rootfs_has() { unsquashfs -l "$IMAGES/rootfs.squashfs" | grep -E "^squashfs-root$1\$"; }
rootfs_cat() { unsquashfs -cat "$IMAGES/rootfs.squashfs" "$1"; }
rootfs_file_contains() { rootfs_cat "$1" | grep -E "$2"; }
boot_has() { mdir -i "$IMAGES/boot.vfat" "::$1"; }
part_count() { [ "$(sfdisk -d "$IMAGES/sdcard.img" | grep -c '^/')" = "$1" ]; }
# sfdisk pads one-digit types (" c"), so compare without whitespace
part_type() { [ "$(sfdisk --part-type "$IMAGES/sdcard.img" "$1" | tr -d '[:space:]')" = "$2" ]; }
usbmode_roundtrip() {  # run the shipped loopi-usbmode against a copy of the shipped config.txt
  local tmp; tmp="$(mktemp -d)"
  rootfs_cat usr/bin/loopi-usbmode > "$tmp/loopi-usbmode" && chmod +x "$tmp/loopi-usbmode"
  cp "$IMAGES/rpi-firmware/config.txt" "$tmp/config.txt"
  local cfg="LOOPI_BOOT_CONFIG=$tmp/config.txt"
  env "$cfg" "$tmp/loopi-usbmode" host \
    && grep -qx '# dtoverlay=dwc2,dr_mode=peripheral' "$tmp/config.txt" \
    && [ "$(env "$cfg" "$tmp/loopi-usbmode")" = host ] \
    && env "$cfg" "$tmp/loopi-usbmode" gadget && env "$cfg" "$tmp/loopi-usbmode" gadget \
    && [ "$(grep -c 'dtoverlay=dwc2' "$tmp/config.txt")" = 1 ] \
    && [ "$(env "$cfg" "$tmp/loopi-usbmode")" = gadget ]
}

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
check "dev image boots in USB gadget mode" grep -qx 'dtoverlay=dwc2,dr_mode=peripheral' "$IMAGES/rpi-firmware/config.txt"
check "dwc2 overlay shipped" boot_has overlays/dwc2.dtbo
check "gadget kernel modules present" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' > /tmp/rootfs.list && grep -q dwc2.ko /tmp/rootfs.list && grep -q usb_f_acm.ko /tmp/rootfs.list && grep -q usb_f_ecm.ko /tmp/rootfs.list"
check "USB gadget service enabled" rootfs_has /etc/systemd/system/multi-user.target.wants/loopi-usbgadget.service
check "loopi-usbmode switches host <-> gadget" usbmode_roundtrip

exit $fail
