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
part_aligned() {  # every partition starts on a multiple of $1 sectors (4 MiB = 8192)
  local starts; starts="$(sfdisk -d "$IMAGES/sdcard.img" | sed -n 's/.*start= *\([0-9]*\),.*/\1/p')"
  [ "$(echo "$starts" | wc -l)" = 4 ] || return 1
  local s; for s in $starts; do [ "$((s % $1))" = 0 ] || return 1; done
}
boot_is_fat32() { file -s "$IMAGES/boot.vfat" | grep -E 'FAT \(32 bit\)'; }
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
check "boot filesystem really is FAT32, not FAT16" boot_is_fat32
check "partitions start on 4 MiB boundaries" part_aligned 8192
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

# --- Task 2: read-only root, data partition, watchdog
check "fstab mounts / read-only squashfs" rootfs_file_contains etc/fstab '^/dev/root +/ +squashfs +ro'
# nofail: if p4 fails fsck/mount, boot must still reach multi-user.target (USB gadget console), not emergency.target
check "fstab mounts /data from p4 (ext4, nofail)" rootfs_file_contains etc/fstab '^/dev/mmcblk0p4 +/data +ext4 +([^ ,]+,)*nofail(,[^ ,]+)* '
check "fstab mounts /var as tmpfs" rootfs_file_contains etc/fstab '^tmpfs +/var +tmpfs'
check "journald is volatile" rootfs_file_contains etc/systemd/journald.conf.d/10-loopi.conf '^Storage=volatile'
check "hardware watchdog enabled" rootfs_file_contains etc/systemd/system.conf.d/10-watchdog.conf '^RuntimeWatchdogSec=15s'
check "data partition grows on first boot" rootfs_has /etc/systemd/system/multi-user.target.wants/loopi-growdata.service
# merged /usr and merged bin: /sbin -> usr/sbin -> bin, so sbin tools are listed under /usr/bin
check "grow tools present" rootfs_has /usr/bin/resize2fs
check "partition tools present (sfdisk, partx)" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' > /tmp/rootfs.list && grep -qx 'squashfs-root/usr/bin/sfdisk' /tmp/rootfs.list && grep -qx 'squashfs-root/usr/bin/partx' /tmp/rootfs.list"
check "/boot and /data mount points exist (read-only root cannot mkdir them)" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' > /tmp/rootfs.list && grep -qx 'squashfs-root/boot' /tmp/rootfs.list && grep -qx 'squashfs-root/data' /tmp/rootfs.list"
ext4_field() { dumpe2fs -h "$IMAGES/data.ext4" 2>/dev/null | sed -n "s/^$1: *//p"; }
data_block_size_is_4k() { [ "$(ext4_field 'Block size')" = 4096 ]; }
# bytes per inode of the base image (resize2fs keeps inodes-per-group when growing); mke2fs' default for a
# 64 MiB image is 4096, which would put ~6% of a 32 GiB card into inode tables
data_inode_ratio_is_high() {
  local blocks bs inodes; blocks="$(ext4_field 'Block count')"; bs="$(ext4_field 'Block size')"; inodes="$(ext4_field 'Inode count')"
  [ -n "$blocks" ] && [ -n "$bs" ] && [ -n "$inodes" ] && [ $((blocks * bs / inodes)) -ge 32768 ]
}
check "data image uses 4 KiB blocks" data_block_size_is_4k
check "data image has a low inode density (>= 32 KiB per inode)" data_inode_ratio_is_high
check "data image is labelled 'data'" bash -c "dumpe2fs -h '$IMAGES/data.ext4' 2>/dev/null | grep -q 'volume name: *data'"

exit $fail
