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
rootfs_lacks() { local list; list="$(unsquashfs -l "$IMAGES/rootfs.squashfs")" || return 1; ! grep -E "^squashfs-root$1\$" <<<"$list"; }
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

# --- Task 3: agent, networking, provisioning
check "agent binary is 32-bit ARM" bash -c "unsquashfs -cat '$IMAGES/rootfs.squashfs' usr/bin/loopi-agent > /tmp/loopi-agent.bin && file /tmp/loopi-agent.bin | grep -q 'ARM, EABI5'"
# The Pi Zero W is an ARMv6 core (ARM1176JZF-S: no NEON, VFPv2): an ARMv7 object that is not behind
# a run-time CPU check would SIGILL there. `readelf -A` on the final agent binary cannot be the test:
# it reports the merged attributes, and ring's seven perlasm objects (<hash>-*-linux32.o: armv4-mont,
# bsaes/vpaes-armv7, chacha/ghash/sha256/sha512-armv4) declare `.arch armv7-a` for their NEON paths, which
# ring selects only after getauxval(AT_HWCAP) reports NEON (run under qemu's arm1176 model it passes, a
# genuine ARMv7 binary gets SIGILL). So the merged tag is always v7. The same guarantee is checked at
# the objects instead: every other C/asm object built for the agent, and the native libraries the agent
# links, must be ARMv6 or older.
cpu_arch_of() { readelf -A "$1" | sed -n 's/^ *Tag_CPU_arch: //p'; }
is_v6_or_older() { case "$(cpu_arch_of "$1")" in v4*|v5*|v6*) return 0 ;; *) echo "not ARMv6: $1" >&2; return 1 ;; esac; }
agent_c_objects_are_armv6() {  # needs the Buildroot build tree next to the images dir (Buildroot's O=)
  local f n=0
  for f in "$IMAGES"/../build/loopi-agent-*/target/*/release/build/*/out/*.o; do
    [ -f "$f" ] || continue
    case "$f" in *-linux32.o) continue ;; esac   # ring perlasm: run-time dispatched, see above
    is_v6_or_older "$f" || return 1
    n=$((n + 1))
  done
  [ "$n" -gt 0 ]   # a glob that matched nothing must not pass
}
agent_native_libs_are_armv6() {  # libopus and libasound as shipped in the rootfs
  local tmp f n=0 rc=0; tmp="$(mktemp -d)"
  unsquashfs -n -d "$tmp/root" "$IMAGES/rootfs.squashfs" 'usr/lib/libopus.so*' 'usr/lib/libasound.so*' >/dev/null || rc=1
  for f in "$tmp"/root/usr/lib/libopus.so.0 "$tmp"/root/usr/lib/libasound.so.2; do
    [ -f "$f" ] && is_v6_or_older "$f" && n=$((n + 1)) || rc=1
  done
  rm -rf "$tmp"; [ "$rc" = 0 ] && [ "$n" = 2 ]
}
check "agent C/asm objects are ARMv6 (ring's run-time dispatched perlasm excepted)" agent_c_objects_are_armv6
check "libopus and libasound shipped for the agent are ARMv6" agent_native_libs_are_armv6
check "agent service enabled" rootfs_has /etc/systemd/system/multi-user.target.wants/loopi-agent.service
check "provisioning enabled" rootfs_has /etc/systemd/system/multi-user.target.wants/loopi-provision.service
check "wifi enabled" rootfs_has /etc/systemd/system/multi-user.target.wants/loopi-wifi.service
check "agent has a systemd watchdog" rootfs_file_contains etc/systemd/system/loopi-agent.service '^WatchdogSec=30'
check "recorders die with the agent" rootfs_file_contains etc/systemd/system/loopi-agent.service '^KillMode=control-group'
check "wlan0 uses DHCP" rootfs_file_contains etc/systemd/network/20-wlan0.network '^DHCP=yes'
check "usb0 serves DHCP to the dev computer" rootfs_file_contains etc/systemd/network/30-usb0.network '^DHCPServer=yes'
check "usb0 does not take over the computer's default route" rootfs_file_contains etc/systemd/network/30-usb0.network '^EmitRouter=no'
check "WiFi firmware present" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' | grep -q brcmfmac43430"
check "BlueZ present" rootfs_has /usr/bin/bluetoothctl
check "timesyncd present" rootfs_has /usr/lib/systemd/systemd-timesyncd
# Added with the controller rulings and the unit paths (not in the brief's list)
check "libcamera log noise is capped at WARN for the agent (and rpicam-vid it spawns)" rootfs_file_contains etc/systemd/system/loopi-agent.service '^Environment=LIBCAMERA_LOG_LEVELS=\*:WARN$'
check "usb0 never holds network-online.target" rootfs_file_contains etc/systemd/network/30-usb0.network '^RequiredForOnline=no$'
# merged /usr and merged bin: the unit says /usr/sbin/wpa_supplicant, which is listed as /usr/bin/wpa_supplicant
check "wpa_supplicant (loopi-wifi ExecStart) is in the image" rootfs_has /usr/bin/wpa_supplicant
# Provisioning writes `ctrl_interface=` into the wpa_supplicant config. A wpa_supplicant built without
# CONFIG_CTRL_IFACE rejects that line as invalid and exits ("Failed to read or parse configuration");
# "ctrl_interface_group" is a config field name that is only compiled in with the control interface.
check "wpa_supplicant accepts ctrl_interface= (control interface compiled in)" bash -c "unsquashfs -cat '$IMAGES/rootfs.squashfs' usr/bin/wpa_supplicant | strings -a | grep -x ctrl_interface_group"
check "stock D-Bus wpa_supplicant.service is not enabled (loopi-wifi owns wlan0)" rootfs_lacks /etc/systemd/system/multi-user.target.wants/wpa_supplicant.service
check "run-agent wrapper is executable" bash -c "unsquashfs -ll '$IMAGES/rootfs.squashfs' | grep -E '^-rwxr-xr-x .* squashfs-root/usr/libexec/loopi/run-agent\$'"
check "dev image marker present" rootfs_has /etc/loopi-dev

exit $fail
