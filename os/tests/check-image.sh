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
# --- ARM1176JZF-S guard. The Pi Zero W core is ARMv6KZ with VFPv2: no Thumb-2, no VFPv3, no NEON. Code that
# uses them traps (SIGILL) unless it is only reached after a run-time CPU feature check.
# `readelf -A` on the final agent binary cannot judge that: it shows the merge over all inputs (v7, VFPv3,
# NEON) and the tag alone says nothing about whether the code is dispatched: ring's perlasm files carry v7
# partly from a top-of-file `.arch armv7-a` that also covers their baseline code. So every input is checked,
# and the only objects allowed to carry v7/VFPv3/NEON are on an exact list of run-time dispatched ones.
#   ring: its NEON paths are entered only after getauxval(AT_HWCAP) reports NEON (neon_available, set in
#     src/cpu/arm.rs; x25519_NEON in src/ec/curve25519/x25519.rs). The Pi Zero W has no NEON.
#   libopus: built with OPUS_HAVE_RTCD and NEON never presumed (config.h: OPUS_ARM_MAY_HAVE_NEON(_INTR),
#     no OPUS_ARM_PRESUME_NEON*); celt/arm/arm_celt_map.c selects the NEON functions from a run-time table.
# Not proven by the tags but by running them: a ring probe over every one of these routines and the opus
# codec ran to completion under qemu-user's `-cpu arm1176` model, where a plain ARMv7 program gets SIGILL.
arm_cpu_ok() { case "$1" in v4|v4T|v5T|v5TE|v5TEJ|v6|v6K|v6KZ) return 0 ;; *) return 1 ;; esac; }  # v6T2 (Thumb-2) is not ARM1176
arm_fp_ok() { case "$1" in ""|No|VFPv1|VFPv2) return 0 ;; *) return 1 ;; esac; }
arm_simd_ok() { case "$1" in ""|No) return 0 ;; *) return 1 ;; esac; }
arm_table() {  # arm_table <file>: "<object>|<cpu arch>|<fp arch>|<simd arch>" per ELF object (archive member) in <file>
  readelf -A "$1" 2>/dev/null | awk -v base="$(basename "$1")" '
    function flush() { if (seen) print name "|" arch "|" fp "|" simd }
    BEGIN { name = base }
    /^File: / { flush(); name = $0; sub(/^File: /, "", name); n = split(name, parts, "/"); name = parts[n]; arch = fp = simd = ""; seen = 0; next }
    $1 == "Tag_CPU_arch:" { arch = $2; seen = 1 }
    $1 == "Tag_FP_arch:" { fp = $2 }
    $1 == "Tag_Advanced_SIMD_arch:" { simd = $2 }
    END { flush() }'
}
arm_dispatched() {  # <object>: exact allowlist (cc crate prefixes every object with a 16 hex digit path hash)
  local o="${1##*(}"; o="${o%)}"
  [[ "$o" =~ ^[0-9a-f]{16}-(.*)$ ]] && o="${BASH_REMATCH[1]}"
  case "$o" in
    armv4-mont-linux32.o|bsaes-armv7-linux32.o|chacha-armv4-linux32.o|ghash-armv4-linux32.o|\
    sha256-armv4-linux32.o|sha512-armv4-linux32.o|vpaes-armv7-linux32.o|x25519-asm-arm.o|poly1305_arm_asm.o) return 0 ;;
    *) return 1 ;;
  esac
}
arm_seen=0
arm_scan() {  # arm_scan <file> <strict|dispatch|cpu-only>: every ELF object in the file within ARM1176 limits
  local obj arch fp simd rc=0
  while IFS='|' read -r obj arch fp simd; do
    arm_seen=$((arm_seen + 1))
    if [ "$2" = cpu-only ]; then arm_cpu_ok "$arch" && continue
    elif arm_cpu_ok "$arch" && arm_fp_ok "$fp" && arm_simd_ok "$simd"; then continue
    elif [ "$2" = dispatch ] && arm_dispatched "$obj"; then continue; fi
    echo "not ARM1176-safe: $1: $obj (arch=$arch fp=${fp:-none} simd=${simd:-none})" >&2; rc=1
  done < <(arm_table "$1")
  return $rc
}
agent_objects_are_armv6() {  # needs the Buildroot O= tree: build/ and host/ next to the images dir
  local b="$IMAGES/../build" f rc=0 out=0 rlib=0 std=0
  arm_seen=0   # C/asm objects the build scripts made (ring), loose and in archives
  for f in "$b"/loopi-agent-*/target/*/release/build/*/out/*.o "$b"/loopi-agent-*/target/*/release/build/*/out/*.a; do
    [ -f "$f" ] && { arm_scan "$f" dispatch || rc=1; }
  done; out=$arm_seen
  arm_seen=0   # the Rust code: every member of every rlib; ring's rlib bundles its C/asm objects
  for f in "$b"/loopi-agent-*/target/*/release/deps/*.rlib; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in libring-*) arm_scan "$f" dispatch || rc=1 ;; *) arm_scan "$f" strict || rc=1 ;; esac
  done; rlib=$arm_seen
  arm_seen=0   # the prebuilt Rust standard library linked into the agent
  for f in "$IMAGES"/../host/lib/rustlib/*/lib/*.rlib; do
    [ -f "$f" ] && { arm_scan "$f" strict || rc=1; }
  done; std=$arm_seen
  [ "$out" -gt 0 ] && [ "$rlib" -gt 0 ] && [ "$std" -gt 0 ] && [ "$rc" = 0 ]   # an empty glob must not pass
}
opus_neon_is_run_time_dispatched() {  # the config.h of the opus that was built into the image
  local c; c="$(ls "$IMAGES"/../build/opus-*/config.h 2>/dev/null | head -n 1)"
  [ -f "$c" ] && grep -Eq '^#define OPUS_HAVE_RTCD 1' "$c" \
    && grep -Eq '^#define OPUS_ARM_MAY_HAVE_NEON' "$c" \
    && ! grep -Eq '^#define OPUS_ARM_PRESUME_(NEON|NEON_INTR|AARCH64_NEON_INTR|DOTPROD)' "$c"
}
agent_native_libs_are_armv6() {  # libopus and libasound as shipped in the rootfs
  local tmp rc=0 opus_mode=strict; tmp="$(mktemp -d)"
  unsquashfs -n -d "$tmp/root" "$IMAGES/rootfs.squashfs" 'usr/lib/libopus.so*' 'usr/lib/libasound.so*' >/dev/null || rc=1
  # libopus is the only shipped library allowed VFPv3/NEON, and only while its NEON is run-time dispatched
  opus_neon_is_run_time_dispatched && opus_mode=cpu-only
  arm_seen=0
  arm_scan "$tmp/root/usr/lib/libasound.so.2" strict || rc=1
  arm_scan "$tmp/root/usr/lib/libopus.so.0" "$opus_mode" || rc=1
  rm -rf "$tmp"; [ "$arm_seen" = 2 ] && [ "$rc" = 0 ]
}
check "agent objects (C/asm, Rust rlibs, std) stay within ARM1176: ARMv6, VFPv2, no NEON; exact run-time dispatch allowlist" agent_objects_are_armv6
check "libasound and libopus shipped for the agent are ARMv6 (libopus NEON only while run-time dispatched)" agent_native_libs_are_armv6
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

# --- Task 4: dev access (key-only SSH, rsync)
check "dropbear enabled" rootfs_has /etc/systemd/system/multi-user.target.wants/dropbear.service
check "SSH host keys persist on /data" bash -c "unsquashfs -ll '$IMAGES/rootfs.squashfs' | grep -q 'etc/dropbear -> /data/dev/dropbear'"
check "authorized_keys live on /data" bash -c "unsquashfs -ll '$IMAGES/rootfs.squashfs' | grep -q 'root/.ssh -> /data/dev/ssh'"
check "SSH keys imported from the boot partition" rootfs_has /etc/systemd/system/multi-user.target.wants/loopi-devkeys.service
check "rsync present" rootfs_has /usr/bin/rsync
check "dev marker present" rootfs_has /etc/loopi-dev
# Added with controller ruling C5 (spec section 6: SSH is key-only). Without it key-only rests on dropbear
# refusing root's empty password, which stops being true the day a root password is set.
dropbear_is_key_only() {
  rootfs_file_contains etc/default/dropbear '^DROPBEAR_ARGS="([^"]* )?-s( [^"]*)?"$' \
    && rootfs_file_contains usr/lib/systemd/system/dropbear.service '^EnvironmentFile=-?/etc/default/dropbear$' \
    && rootfs_file_contains usr/lib/systemd/system/dropbear.service '^ExecStart=.* \$DROPBEAR_ARGS( |$)'
}
check "dropbear refuses password logins (-s from /etc/default/dropbear reaches its command line)" dropbear_is_key_only
# tmpfiles.d is only best effort for /data. A nofail mount is not ordered before local-fs.target (systemd.mount(5)),
# so systemd-tmpfiles-setup.service normally runs before /data is mounted and acts on the squashfs mount point,
# not on the ext4 that is mounted over it. What guarantees the SSH state directories is the drop-in below
# (dropbear) and loopi-devkeys.service (authorized_keys), both of which run after /data is mounted.
tmpfiles_make_ssh_dirs() {
  rootfs_file_contains etc/tmpfiles.d/loopi.conf '^d /data/dev/dropbear +0700 root root' \
    && rootfs_file_contains etc/tmpfiles.d/loopi.conf '^d /data/dev/ssh +0700 root root'
}
check "tmpfiles.d declares the SSH state directories (best effort: runs before the nofail /data mount)" tmpfiles_make_ssh_dirs
# Fix round 1 (ruling C26). `dropbear -R` creates its host key with open(O_CREAT|O_EXCL): without /data/dev/dropbear
# it fails with ENOENT ("Couldn't read or generate hostkey") and every SSH connection dies. So dropbear.service must
# wait for /data (RequiresMountsFor, as every unit that needs /data) and create the directory itself.
unit_keys() {  # unit_keys <path in rootfs>: one "[Section] Key=Value" line per setting (a key in the wrong section is ignored by systemd)
  local keys; keys="$(rootfs_cat "$1")" || return 1
  awk '/^\[/ { s = $0; next } NF && !/^[#;]/ { print s " " $0 }' <<<"$keys"
}
dropbear_hostkey_dir_after_mount() {
  local keys; keys="$(unit_keys etc/systemd/system/dropbear.service.d/10-loopi.conf)" || return 1
  grep -Fx '[Unit] RequiresMountsFor=/data' <<<"$keys" \
    && grep -Fx '[Service] ExecStartPre=/bin/mkdir -p -m 0700 /data/dev/dropbear' <<<"$keys"
}
check "dropbear waits for /data and creates /data/dev/dropbear itself (drop-in)" dropbear_hostkey_dir_after_mount
# /bin -> usr/bin and mkdir is a busybox applet link there; the drop-in's ExecStartPre needs it
check "mkdir for the dropbear drop-in is in the image" rootfs_has /usr/bin/mkdir

# --- Task 5: camera and USB microphone
check "rpicam-vid present" rootfs_has /usr/bin/rpicam-vid
check "libcamera Raspberry Pi IPA present" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' | grep -q 'ipa_rpi_vc4'"
check "USB audio driver present" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' | grep -q 'snd-usb-audio.ko'"
check "camera auto-detect enabled" grep -qx 'camera_auto_detect=1' "$IMAGES/rpi-firmware/config.txt"
check "onboard audio disabled" grep -qx 'dtparam=audio=off' "$IMAGES/rpi-firmware/config.txt"
check "KMS overlay shipped" boot_has overlays/vc4-kms-v3d.dtbo
check "OV5647 overlay shipped" boot_has overlays/ov5647.dtbo
check "arecord present (dev)" rootfs_has /usr/bin/arecord

# --- Fix C1 (ruling C28): rpicam-apps v1.7.0 replaces Request::controls() with its own ControlList, which upstream
# libcamera >= 0.7 refuses at queueRequest ("Overwriting Request::controls() is not allowed"), so rpicam-vid aborts with
# "failed to queue request". os/patches/libcamera-apps/0001-*.patch (BR2_GLOBAL_PATCH_DIR) merges the pending controls
# into the request's own list instead. The patched source is in the Buildroot O= tree (build/ next to the images dir).
rpicam_request_controls_are_merged() {
  local d f; d="$(ls -d "$IMAGES"/../build/libcamera-apps-*/ 2>/dev/null | head -n 1)"; f="$d/core/rpicam_app.cpp"
  [ -f "$f" ] && [ -s "$d/.applied_patches_list" ] \
    && grep -Eq '/os/patches/libcamera-apps/0001-[^/]*\.patch$' "$d/.applied_patches_list" \
    && grep -Eq 'request->controls\(\)\.merge\(controls_, ControlList::MergePolicy::OverwriteExisting\);' "$f" \
    && ! grep -Eq 'request->controls\(\) *= ' "$f"   # the replacing assignment is gone; an unpatched tree must not pass
}
check "rpicam-apps merges its controls into the request's own list (upstream libcamera 0.7 refuses replacing it)" rpicam_request_controls_are_merged

exit $fail
