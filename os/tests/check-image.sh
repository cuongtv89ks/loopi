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
# unit_keys <path in rootfs>: one "[Section] Key=Value" line per setting (a key in the wrong section is ignored by systemd)
unit_keys() {
  local keys; keys="$(rootfs_cat "$1")" || return 1
  awk '/^\[/ { s = $0; next } NF && !/^[#;]/ { print s " " $0 }' <<<"$keys"
}
boot_has() { mdir -i "$IMAGES/boot.vfat" "::$1"; }
boot_cat() { mtype -i "$IMAGES/boot.vfat" "::$1"; }
# boot_cfg_has <line>: config.txt in the boot partition has exactly this line (plain grep, see the note above)
boot_cfg_has() { boot_cat config.txt | grep -x -- "$1"; }
# base_dtparam <param>: config.txt in the boot partition sets dtparam=<param> before its first dtoverlay= line (a dtparam=
# after an overlay is looked up in that overlay first, so a base DT parameter belongs above them)
base_dtparam() {
  local cfg; cfg="$(boot_cat config.txt)" || return 1
  awk -v want="dtparam=$1" '/^dtoverlay=/ { exit } $0 == want { found = 1 } END { exit !found }' <<<"$cfg"
}
dtb_dts() {  # the Zero W DTB in the boot partition, decompiled
  local tmp rc=1; tmp="$(mktemp -d)" || return 1
  mcopy -n -i "$IMAGES/boot.vfat" ::a/bcm2708-rpi-zero-w.dtb "$tmp/zero-w.dtb" \
    && "$IMAGES/../host/bin/dtc" -q -I dtb -O dts "$tmp/zero-w.dtb" && rc=0
  rm -rf "$tmp"; return $rc
}
# dtb_override <name> <ERE>: the shipped DTB defines dtparam <name>; its decompiled __overrides__ entry matches
# "<name> = <ERE>;". A parameter the DTB does not define is ignored by the firmware without a word.
dtb_override() { local dts; dts="$(dtb_dts)" || return 1; grep -E "^[[:space:]]*$1 = $2;\$" <<<"$dts"; }
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
  boot_cat config.txt > "$tmp/config.txt"
  local cfg="LOOPI_BOOT_CONFIG=$tmp/config.txt"
  env "$cfg" "$tmp/loopi-usbmode" host \
    && grep -qx '# dtoverlay=dwc2,dr_mode=peripheral' "$tmp/config.txt" \
    && [ "$(env "$cfg" "$tmp/loopi-usbmode")" = host ] \
    && env "$cfg" "$tmp/loopi-usbmode" gadget && env "$cfg" "$tmp/loopi-usbmode" gadget \
    && [ "$(grep -c 'dtoverlay=dwc2' "$tmp/config.txt")" = 1 ] \
    && [ "$(env "$cfg" "$tmp/loopi-usbmode")" = gadget ]
}

# Variant (spec §6.5): the dev marker /etc/loopi-dev is only in rootfs-overlay-dev. Checks that depend on the dev overlay or
# on dev.fragment run under is_dev; prod has its own checks at the end.
variant=dev
rootfs_has /etc/loopi-dev >/dev/null 2>&1 || variant=prod
echo "variant: $variant"
is_dev() { [ "$variant" = dev ]; }

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
# P4a: the kernel command line of slot A is a/cmdline.txt, rendered from the template by loopi-boot install-files
check "cmdline mounts squashfs read-only" bash -c "mtype -i '$IMAGES/boot.vfat' ::a/cmdline.txt | grep -q 'root=/dev/mmcblk0p2 rootfstype=squashfs ro rootwait'"
# fsck.repair=yes (ruling I-3, owner decision): systemd-fsck answers "yes" to every question at boot instead of only
# preening, so /data and /boot damaged by a power cut get repaired, even when that deletes already-damaged files.
cmdline_is_one_line_with() { [ "$(boot_cat a/cmdline.txt | grep -c '')" = 1 ] && boot_cat a/cmdline.txt | grep -E "$1"; }
check "cmdline is one line and repairs filesystems at boot (fsck.repair=yes)" cmdline_is_one_line_with '(^| )fsck\.repair=yes( |$)'
check "boot partition has config.txt" boot_has config.txt
check "boot partition has loopi.toml.example" boot_has loopi.toml.example
if is_dev; then
  check "dev image boots in USB gadget mode" boot_cfg_has 'dtoverlay=dwc2,dr_mode=peripheral'
fi
check "dwc2 overlay shipped" boot_has a/overlays/dwc2.dtbo
check "gadget kernel modules present" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' > /tmp/rootfs.list && grep -q dwc2.ko /tmp/rootfs.list && grep -q usb_f_acm.ko /tmp/rootfs.list && grep -q usb_f_ecm.ko /tmp/rootfs.list"
if is_dev; then
  check "USB gadget service enabled" rootfs_has /etc/systemd/system/multi-user.target.wants/loopi-usbgadget.service
  check "loopi-usbmode switches host <-> gadget" usbmode_roundtrip
fi

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
if is_dev; then
  check "usb0 serves DHCP to the dev computer" rootfs_file_contains etc/systemd/network/30-usb0.network '^DHCPServer=yes'
  check "usb0 does not take over the computer's default route" rootfs_file_contains etc/systemd/network/30-usb0.network '^EmitRouter=no'
fi
check "WiFi firmware present" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' | grep -q brcmfmac43430"
check "BlueZ present" rootfs_has /usr/bin/bluetoothctl
check "timesyncd present" rootfs_has /usr/lib/systemd/systemd-timesyncd
# Added with the controller rulings and the unit paths (not in the brief's list)
check "libcamera log noise is capped at WARN for the agent (and rpicam-vid it spawns)" rootfs_file_contains etc/systemd/system/loopi-agent.service '^Environment=LIBCAMERA_LOG_LEVELS=\*:WARN$'
if is_dev; then
  check "usb0 never holds network-online.target" rootfs_file_contains etc/systemd/network/30-usb0.network '^RequiredForOnline=no$'
fi
# merged /usr and merged bin: the unit says /usr/sbin/wpa_supplicant, which is listed as /usr/bin/wpa_supplicant
check "wpa_supplicant (loopi-wifi ExecStart) is in the image" rootfs_has /usr/bin/wpa_supplicant
# Provisioning writes `ctrl_interface=` into the wpa_supplicant config. A wpa_supplicant built without
# CONFIG_CTRL_IFACE rejects that line as invalid and exits ("Failed to read or parse configuration");
# "ctrl_interface_group" is a config field name that is only compiled in with the control interface.
check "wpa_supplicant accepts ctrl_interface= (control interface compiled in)" bash -c "unsquashfs -cat '$IMAGES/rootfs.squashfs' usr/bin/wpa_supplicant | strings -a | grep -x ctrl_interface_group"
check "stock D-Bus wpa_supplicant.service is not enabled (loopi-wifi owns wlan0)" rootfs_lacks /etc/systemd/system/multi-user.target.wants/wpa_supplicant.service
check "run-agent wrapper is executable" bash -c "unsquashfs -ll '$IMAGES/rootfs.squashfs' | grep -E '^-rwxr-xr-x .* squashfs-root/usr/libexec/loopi/run-agent\$'"
if is_dev; then
  check "dev image marker present" rootfs_has /etc/loopi-dev
fi

# --- Task 4: dev access (key-only SSH, rsync)
if is_dev; then
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
  dropbear_hostkey_dir_after_mount() {
    local keys; keys="$(unit_keys etc/systemd/system/dropbear.service.d/10-loopi.conf)" || return 1
    grep -Fx '[Unit] RequiresMountsFor=/data' <<<"$keys" \
      && grep -Fx '[Service] ExecStartPre=/bin/mkdir -p -m 0700 /data/dev/dropbear' <<<"$keys"
  }
  check "dropbear waits for /data and creates /data/dev/dropbear itself (drop-in)" dropbear_hostkey_dir_after_mount
  # /bin -> usr/bin and mkdir is a busybox applet link there; the drop-in's ExecStartPre needs it
  check "mkdir for the dropbear drop-in is in the image" rootfs_has /usr/bin/mkdir
fi

# --- Task 5: camera and USB microphone
check "rpicam-vid present" rootfs_has /usr/bin/rpicam-vid
check "libcamera Raspberry Pi IPA present" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' | grep -q 'ipa_rpi_vc4'"
check "USB audio driver present" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' | grep -q 'snd-usb-audio.ko'"
check "camera auto-detect enabled" boot_cfg_has 'camera_auto_detect=1'
check "onboard audio disabled" boot_cfg_has 'dtparam=audio=off'
check "KMS overlay shipped" boot_has a/overlays/vc4-kms-v3d.dtbo
check "OV5647 overlay shipped" boot_has a/overlays/ov5647.dtbo
if is_dev; then
  check "arecord present (dev)" rootfs_has /usr/bin/arecord
fi

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

# --- Fix F1 (ruling C30): the boot FAT must survive the frequent power cuts of this device.
# Observed: power pulled during first-boot provisioning (which rewrites /boot/loopi.toml) left the FAT dirty, the next
# boot had /boot remounted read-only by a FAT error and `loopi-usbmode host` failed. The image had no fsck.fat, so the
# damage was never repaired and Linux (which keeps the dirty flag it found at mount time) warned on every later boot.
#   (a) repair at boot: fstab pass 2 makes systemd-fsck@dev-mmcblk0p1.service run `fsck -a`, which needs fsck.vfat;
#   (b) exposure: /boot is mounted read-only and opened for writing only while the system writes it.
# systemd-fsck runs util-linux `fsck`, which searches for the fsck.<type> helper only in the directories of $PATH
# (its built-in default of /sbin applies only when PATH is unset); systemd's own lookup (generator and systemd-fsck) and the
# PATH it gives its services (/usr/local/sbin:/usr/local/bin:/usr/bin on a merged-bin rootfs) both contain /usr/bin.
# /usr/sbin and /sbin are links to bin here (merged bin), so the helper must resolve to the real fsck.fat through them.
fsck_vfat_helper_resolves() {
  local tmp rc=0; tmp="$(mktemp -d)"
  unsquashfs -n -d "$tmp/root" "$IMAGES/rootfs.squashfs" sbin usr/sbin usr/bin/fsck usr/bin/fsck.fat usr/bin/fsck.vfat >/dev/null || rc=1
  if [ "$rc" = 0 ]; then
    local real; real="$(readlink -f "$tmp/root/usr/sbin/fsck.vfat")"
    [ "$real" = "$(readlink -f "$tmp/root/usr/bin/fsck.fat")" ] && [ -x "$real" ] \
      && [ "$(readlink -f "$tmp/root/sbin/fsck.vfat")" = "$real" ] \
      && [ -x "$tmp/root/usr/bin/fsck" ] && [ "$(readlink -f "$tmp/root/usr/sbin/fsck")" = "$(readlink -f "$tmp/root/usr/bin/fsck")" ] \
      && file -L "$real" | grep -E 'ELF 32-bit .*ARM' || rc=1
  fi
  rm -rf "$tmp"; return $rc
}
check "fsck.vfat -> fsck.fat (ARM) is in the image and resolves from /sbin and /usr/sbin next to the util-linux fsck" fsck_vfat_helper_resolves
# A pass of 0 would make systemd-fstab-generator skip the check; the generator also skips it when no fsck.vfat exists.
# `ro` keeps the FAT clean (the dirty flag is set only by a read-write mount); nofail keeps a /boot that cannot be
# mounted (or whose fsck fails) from stopping the boot: nofail mounts are only Wants= of local-fs.target.
fstab_boot_is_ro_and_checked() {
  rootfs_cat etc/fstab | awk '
    $1 !~ /^#/ && $2 == "/boot" {
      found = 1; n = split($4, o, ","); for (i = 1; i <= n; i++) opt[o[i]] = 1
      ok = ($1 == "/dev/mmcblk0p1" && $3 == "vfat" && ("ro" in opt) && !("rw" in opt) && !("defaults" in opt) \
            && ("nofail" in opt) && ("noatime" in opt) && $6 == 2)
    }
    END { exit !(found && ok) }'
}
check "fstab mounts /boot read-only with fsck pass 2 (and nofail)" fstab_boot_is_ro_and_checked
# loopi-provision.service is the only writer of /boot at run time: `loopi-agent provision` scrubs the secrets from
# /boot/loopi.toml. rw remount before; ro after on success (ExecStartPost) and on every failure path (ExecStopPost).
# The ro lines are `-` prefixed and log their own failure, so a failed final remount never fails the unit.
provision_opens_and_closes_boot_window() {
  local keys; keys="$(unit_keys etc/systemd/system/loopi-provision.service)" || return 1
  grep -Fx '[Service] ExecStartPre=/usr/bin/mount -o remount,rw /boot' <<<"$keys" \
    && grep -Fx '[Service] ExecStart=/usr/bin/loopi-agent provision --boot /boot/loopi.toml --data /data' <<<"$keys" \
    && grep -E '^\[Service\] ExecStartPost=-.*mount -o remount,ro /boot .*\|\| .*echo .*>&2' <<<"$keys" \
    && grep -E '^\[Service\] ExecStopPost=-.*mount -o remount,ro /boot .*\|\| .*echo .*>&2' <<<"$keys" \
    && grep -Fx '[Unit] RequiresMountsFor=/data /boot' <<<"$keys" \
    && grep -Fx '[Unit] ConditionPathExists=/boot/loopi.toml' <<<"$keys" \
    && grep -Fx '[Unit] Before=loopi-wifi.service loopi-agent.service' <<<"$keys" \
    && grep -Fx '[Service] Type=oneshot' <<<"$keys"
}
check "loopi-provision.service opens /boot read-write only around the provisioning (ro again on success and on failure)" provision_opens_and_closes_boot_window
if is_dev; then
  # loopi-devkeys.service only reads /boot (install -m 0600 /boot/loopi-authorized_keys /data/...): it must stay without a
  # remount, i.e. nothing may open /boot for writing for a read.
  devkeys_only_reads_boot() {
    rootfs_file_contains etc/systemd/system/loopi-devkeys.service "install -m 0600 /boot/loopi-authorized_keys /data/dev/ssh/authorized_keys" \
      && ! rootfs_cat etc/systemd/system/loopi-devkeys.service | grep -E 'remount'
  }
  check "loopi-devkeys.service only reads /boot (copies the key out, no remount)" devkeys_only_reads_boot
  # The shipped loopi-usbmode against a fake boot mount point: stub `mountpoint` (true for the fake /boot only) and `mount`
  # (logs its arguments and whether the dwc2 line was active at that moment, can be told to fail). The real mount is never
  # called, so this is safe on the build host; usbmode_roundtrip above still runs the script without any stub.
  usbmode_boot_window() {
    local tmp rc=0; tmp="$(mktemp -d)" || return 1
    mkdir "$tmp/bin" "$tmp/boot" "$tmp/plain"
    rootfs_cat usr/bin/loopi-usbmode > "$tmp/loopi-usbmode" && chmod +x "$tmp/loopi-usbmode" || { rm -rf "$tmp"; return 1; }
    printf '#!/bin/sh\n[ "$1" = -q ] && [ "$2" = "%s" ]\n' "$tmp/boot" > "$tmp/bin/mountpoint"
    printf '#!/bin/sh\necho "$* [active=$(grep -c "^dtoverlay=dwc2" "%s/boot/config.txt" 2>/dev/null)]" >> "%s/mount.log"\ncase "$*" in *"${MOCK_FAIL:-NONE}"*) exit 32 ;; esac\n' \
      "$tmp" "$tmp" > "$tmp/bin/mount"
    chmod +x "$tmp/bin/mountpoint" "$tmp/bin/mount"
    local line='dtoverlay=dwc2,dr_mode=peripheral'
    um() { env PATH="$tmp/bin:$PATH" "$@"; }
    reset() { rm -f "$tmp/mount.log"; printf 'camera_auto_detect=1\n%s\n' "$1" > "$tmp/boot/config.txt"; cp "$tmp/boot/config.txt" "$tmp/plain/config.txt"; }
    # 1. the switch: rw first, the edit inside the window (line still active at rw, gone at ro), ro last
    reset "$line"
    um LOOPI_BOOT_CONFIG="$tmp/boot/config.txt" "$tmp/loopi-usbmode" host >/dev/null 2>&1 \
      && [ "$(cat "$tmp/mount.log")" = "$(printf -- '-o remount,rw %s/boot [active=1]\n-o remount,ro %s/boot [active=0]' "$tmp" "$tmp")" ] \
      && grep -qx "# $line" "$tmp/boot/config.txt" || rc=1
    # 1b. and back: the edit again lies between the two remounts (line commented at rw, active at ro)
    um LOOPI_BOOT_CONFIG="$tmp/boot/config.txt" "$tmp/loopi-usbmode" gadget >/dev/null 2>&1 \
      && [ "$(tail -n 2 "$tmp/mount.log")" = "$(printf -- '-o remount,rw %s/boot [active=0]\n-o remount,ro %s/boot [active=1]' "$tmp" "$tmp")" ] \
      && grep -qx "$line" "$tmp/boot/config.txt" || rc=1
    # 2. a failing edit still ends with the window closed and a non-zero exit: (a) no line to enable, (b) sed itself fails
    # (config.txt missing), which stops the script inside the window, so only the EXIT trap can close it
    reset "# something else"
    um LOOPI_BOOT_CONFIG="$tmp/boot/config.txt" "$tmp/loopi-usbmode" gadget >/dev/null 2>&1 && rc=1
    [ "$(tail -n 1 "$tmp/mount.log" 2>/dev/null)" = "-o remount,ro $tmp/boot [active=0]" ] || rc=1
    reset "$line"; rm -f "$tmp/boot/config.txt"
    um LOOPI_BOOT_CONFIG="$tmp/boot/config.txt" "$tmp/loopi-usbmode" host >/dev/null 2>&1 && rc=1
    [ "$(sed -n '1p;$p' "$tmp/mount.log" 2>/dev/null)" = "$(printf -- '-o remount,rw %s/boot [active=]\n-o remount,ro %s/boot [active=]' "$tmp" "$tmp")" ] || rc=1
    # 3. cannot open the window: nothing is edited, non-zero exit
    reset "$line"
    um MOCK_FAIL=remount,rw LOOPI_BOOT_CONFIG="$tmp/boot/config.txt" "$tmp/loopi-usbmode" host >/dev/null 2>&1 && rc=1
    cmp -s "$tmp/boot/config.txt" "$tmp/plain/config.txt" || rc=1
    # 4. cannot close it again: the switch is done and reported, the warning is printed
    reset "$line"
    um MOCK_FAIL=remount,ro LOOPI_BOOT_CONFIG="$tmp/boot/config.txt" "$tmp/loopi-usbmode" host >/dev/null 2>"$tmp/err" \
      && grep -q 'read-only' "$tmp/err" && grep -qx "# $line" "$tmp/boot/config.txt" || rc=1
    # 5. reading the mode and a config that is not on a mount point never call mount
    reset "$line"
    um LOOPI_BOOT_CONFIG="$tmp/boot/config.txt" "$tmp/loopi-usbmode" >/dev/null 2>&1 || rc=1
    um LOOPI_BOOT_CONFIG="$tmp/plain/config.txt" "$tmp/loopi-usbmode" host >/dev/null 2>&1 || rc=1
    um LOOPI_BOOT_CONFIG="$tmp/plain/config.txt" "$tmp/loopi-usbmode" bogus >/dev/null 2>&1 && rc=1
    [ ! -e "$tmp/mount.log" ] || rc=1
    grep -qx "# $line" "$tmp/plain/config.txt" || rc=1
    rm -rf "$tmp"; return $rc
  }
  check "loopi-usbmode opens its mount point read-write only for the edit, closes it also on error, never mounts off /boot" usbmode_boot_window
fi

# --- Final fix C-1: the Raspberry Pi kernel keeps a released GPIO's output level (pinctrl-bcm2835 persist_gpio_outputs;
# dmesg "GPIO_OUT persistence: yes"), so an LED on a GPIO stayed lit after the agent was killed (SIGKILL, abort, watchdog).
# dtparam=strict_gpiod (bcm270x-rpi.dtsi) appends pinctrl_bcm2835.persist_gpio_outputs=n to the kernel command line.
strict_gpiod_enabled() {
  base_dtparam strict_gpiod && dtb_override strict_gpiod '.*bootargs=pinctrl_bcm2835\.persist_gpio_outputs=n"'
}
check "released GPIOs return to inputs (dtparam=strict_gpiod before any overlay; the shipped DTB defines it)" strict_gpiod_enabled

# --- Final fix D18: the green ACT LED is the privacy LED. Only the agent drives it: act_led_trigger=none (a base parameter
# of the Zero W DTB) removes the kernel's default trigger, actpwr. The agent's default LED is /sys/class/leds/ACT, the label
# of the DTB's led-act node. A LED class device keeps its brightness when its writer dies, so the agent unit switches it
# off after every exit (ExecStopPost runs once the control group, recorders included, is gone) with the shipped binary.
act_led_has_no_kernel_trigger() {
  base_dtparam act_led_trigger=none && dtb_override act_led_trigger '.*linux,default-trigger"'
}
act_led_is_labelled_ACT() {
  local dts; dts="$(dtb_dts)" || return 1
  awk '/^[[:space:]]*led-act \{$/ { inside = 1; next }
       inside && /^[[:space:]]*\};$/ { inside = 0 }
       inside && /^[[:space:]]*label = "ACT";$/ { found = 1 }
       END { exit !found }' <<<"$dts"
}
agent_unit_switches_the_led_off_after_any_exit() {
  local keys; keys="$(unit_keys etc/systemd/system/loopi-agent.service)" || return 1
  grep -Fx '[Service] ExecStopPost=-/usr/bin/loopi-agent led-off' <<<"$keys" \
    && grep -Fx '[Service] KillMode=control-group' <<<"$keys"
}
agent_binary_knows_led_off_and_the_act_led() {
  local text; text="$(rootfs_cat usr/bin/loopi-agent | strings -a)" || return 1
  grep -F 'selftest | led-off' <<<"$text" && grep -F '/sys/class/leds/ACT' <<<"$text"
}
check "no kernel trigger drives the ACT LED (dtparam=act_led_trigger=none before any overlay; the DTB defines it)" act_led_has_no_kernel_trigger
check "the DTB names the ACT LED \"ACT\" (the agent's default /sys/class/leds/ACT)" act_led_is_labelled_ACT
check "the agent unit switches the privacy LED off after any agent exit (ExecStopPost=-/usr/bin/loopi-agent led-off)" agent_unit_switches_the_led_off_after_any_exit
check "the shipped agent has the led-off command and defaults to /sys/class/leds/ACT" agent_binary_knows_led_off_and_the_act_led

# --- Final fix I-2: loopi-wifi.service BindsTo= wlan0, so a brcmfmac reset or module reload (wlan0 removed and created
# again) stops it, and being wanted only by multi-user.target nothing started it again. The wlan0 device now wants it.
wifi_follows_wlan0() {
  local keys; keys="$(unit_keys etc/systemd/system/loopi-wifi.service)" || return 1
  grep -Fx '[Unit] BindsTo=sys-subsystem-net-devices-wlan0.device' <<<"$keys" \
    && grep -Ex '\[Install\] WantedBy=(.* )?sys-subsystem-net-devices-wlan0\.device( .*)?' <<<"$keys" \
    && unsquashfs -ll "$IMAGES/rootfs.squashfs" | grep -E \
      ' squashfs-root/etc/systemd/system/sys-subsystem-net-devices-wlan0\.device\.wants/loopi-wifi\.service -> (\.\./|/etc/systemd/system/)loopi-wifi\.service$'
}
check "WiFi starts again when wlan0 reappears (wanted by the wlan0 device, link in its .wants)" wifi_follows_wlan0
# M-7: the machine ID is new at every boot (read-only root), and so is the default DUID-based DHCP client identifier.
wlan0_dhcp_identity_is_the_mac() {
  local keys; keys="$(unit_keys etc/systemd/network/20-wlan0.network)" || return 1
  grep -Fx '[DHCPv4] ClientIdentifier=mac' <<<"$keys"
}
check "wlan0 asks DHCP with its MAC as client identifier (same IP across power cuts)" wlan0_dhcp_identity_is_the_mac

# --- Final fix G: cheap gaps. The agent unit's lifecycle settings (notify + watchdog, restart, /data, provisioning
# first), the usb0 DHCP server not handing out a DNS server, /data checked at boot (fsck pass 2), and the tools the
# units and loopi-usbmode call by path or name.
agent_unit_lifecycle() {
  local keys; keys="$(unit_keys etc/systemd/system/loopi-agent.service)" || return 1
  grep -Fx '[Service] Type=notify' <<<"$keys" && grep -Fx '[Service] Restart=always' <<<"$keys" \
    && grep -Fx '[Unit] RequiresMountsFor=/data' <<<"$keys" \
    && grep -Ex '\[Unit\] After=(.* )?loopi-provision\.service( .*)?' <<<"$keys"
}
check "agent unit: Type=notify, Restart=always, RequiresMountsFor=/data, after loopi-provision" agent_unit_lifecycle
if is_dev; then
  usb0_emits_no_dns() {
    local keys; keys="$(unit_keys etc/systemd/network/30-usb0.network)" || return 1
    grep -Fx '[DHCPServer] EmitDNS=no' <<<"$keys"
  }
  check "usb0 DHCP server hands out no DNS server (the computer keeps its own)" usb0_emits_no_dns
fi
fstab_data_is_checked() {
  rootfs_cat etc/fstab | awk '$1 !~ /^#/ && $2 == "/data" { found = 1; ok = ($1 == "/dev/mmcblk0p4" && $6 == 2) }
                             END { exit !(found && ok) }'
}
check "fstab checks /data at boot (fsck pass 2)" fstab_data_is_checked
check "mount and mountpoint are in the image (loopi-provision, loopi-usbmode)" bash -c \
  "unsquashfs -l '$IMAGES/rootfs.squashfs' > /tmp/rootfs.list && grep -qx 'squashfs-root/usr/bin/mount' /tmp/rootfs.list && grep -qx 'squashfs-root/usr/bin/mountpoint' /tmp/rootfs.list"
provision_window_is_bounded() {
  local keys; keys="$(unit_keys etc/systemd/system/loopi-provision.service)" || return 1
  grep -Fx '[Service] TimeoutStartSec=60' <<<"$keys"
}
check "loopi-provision gives up after 60 s (it holds the /boot write window)" provision_window_is_bounded
if is_dev; then
  # loopi-usbmode without LOOPI_BOOT_CONFIG while /boot is not mounted (nofail leaves an unrepairable FAT unmounted): every
  # mode fails with a pointer to the fsck log and touches nothing; a config that cannot be read is an error, never "host".
  # `mountpoint` is stubbed to say no for every path and `mount` only logs, so the build host's /boot is never touched.
  usbmode_fails_closed() {
    local tmp rc=0 out mode; tmp="$(mktemp -d)" || return 1
    mkdir "$tmp/bin"
    rootfs_cat usr/bin/loopi-usbmode > "$tmp/loopi-usbmode" && chmod +x "$tmp/loopi-usbmode" || { rm -rf "$tmp"; return 1; }
    printf '#!/bin/sh\nexit 1\n' > "$tmp/bin/mountpoint"
    printf '#!/bin/sh\necho "$*" >> "%s/mount.log"\n' "$tmp" > "$tmp/bin/mount"
    chmod +x "$tmp/bin/mountpoint" "$tmp/bin/mount"
    for mode in "" host gadget; do
      out="$(env -u LOOPI_BOOT_CONFIG PATH="$tmp/bin:$PATH" "$tmp/loopi-usbmode" ${mode:+"$mode"} 2>"$tmp/err")" && rc=1
      [ -z "$out" ] && grep -qF '/boot is not mounted' "$tmp/err" \
        && grep -qF 'journalctl -u systemd-fsck@dev-mmcblk0p1' "$tmp/err" || rc=1
    done
    [ ! -e "$tmp/mount.log" ] || rc=1
    out="$(env PATH="$tmp/bin:$PATH" LOOPI_BOOT_CONFIG="$tmp/missing/config.txt" "$tmp/loopi-usbmode" 2>"$tmp/err")" && rc=1
    [ -z "$out" ] && grep -qF "cannot read $tmp/missing/config.txt" "$tmp/err" || rc=1
    rm -rf "$tmp"; return $rc
  }
  check "loopi-usbmode fails closed: /boot not mounted -> error in every mode; unreadable config -> error, not \"host\"" usbmode_fails_closed
fi

# --- Audio level (spec D19): the agent amplifies the USB microphone before encoding (capture.audio_gain_db). The agent is
# a local Buildroot package, rsynced only on a fresh build: an image made without `loopi-agent-dirclean` silently ships the
# previous agent, and this check catches that.
agent_binary_amplifies_the_microphone() {
  local text; text="$(rootfs_cat usr/bin/loopi-agent | strings -a)" || return 1
  grep -F 'capture.audio_gain_db must be 0..=' <<<"$text"
}
example_sets_the_microphone_gain() { boot_cat loopi.toml.example | grep -E '^audio_gain_db = 20\.0 '; }
check "the shipped agent amplifies the microphone before encoding (capture.audio_gain_db, spec D19)" agent_binary_amplifies_the_microphone
check "loopi.toml.example shows the microphone gain (audio_gain_db = 20.0)" example_sets_the_microphone_gain

# --- P4a: version stamp and the prod variant (spec §6.5)
# run-agent starts /data/dev/loopi-agent only behind this guard. /data is shared by both variants (a device can go dev -> prod
# over OTA with a deployed /data/dev/loopi-agent left behind), so on prod, which has no /etc/loopi-dev, the guard is the
# only thing that keeps that binary from running.
run_agent_guards_data_dev() { rootfs_cat usr/libexec/loopi/run-agent | grep -Fx 'if [ -f /etc/loopi-dev ] && [ -x /data/dev/loopi-agent ]; then'; }
check "/etc/loopi-release names this build" bash -c "unsquashfs -cat '$IMAGES/rootfs.squashfs' etc/loopi-release | grep -qx 'LOOPI_OS_VERSION=${LOOPI_OS_VERSION}' && unsquashfs -cat '$IMAGES/rootfs.squashfs' etc/loopi-release | grep -qx 'LOOPI_OS_VARIANT=$variant'"
if ! is_dev; then
  check "prod: no SSH server" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' > /tmp/rootfs.list && ! grep -Eq '/(dropbear|sshd)$' /tmp/rootfs.list"
  check "prod: no dev marker, gadget unit or usbmode" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' > /tmp/rootfs.list && ! grep -Eq 'etc/loopi-dev$|loopi-usbgadget\.service$|usr/bin/loopi-usbmode$|loopi-devkeys\.service$|etc/tmpfiles\.d/loopi\.conf$' /tmp/rootfs.list"
  check "prod: no debug tools" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' > /tmp/rootfs.list && ! grep -Eq '/(strace|rsync|arecord|aplay)$' /tmp/rootfs.list"
fi
check "agent wrapper runs /data/dev only when /etc/loopi-dev exists" run_agent_guards_data_dev

# --- P4a: A/B slots (spec D26) and signed bundles
check "config.txt boots slot A" boot_cfg_has 'os_prefix=a/'
check "no tryboot.txt in a fresh image" bash -c "! mdir -i '$IMAGES/boot.vfat' ::tryboot.txt"
check "slot A has kernel, DTB, overlays (and the README the firmware needs to use them), cmdline and template" bash -c "for f in zImage bcm2708-rpi-zero-w.dtb overlays/README overlays/dwc2.dtbo cmdline.txt loopi-config.txt; do mdir -i '$IMAGES/boot.vfat' ::a/\$f || exit 1; done"
check "slot A cmdline: p2, rauc.slot=A, panic=10" bash -c "mtype -i '$IMAGES/boot.vfat' ::a/cmdline.txt | grep -Eq '^root=/dev/mmcblk0p2 .* rauc\.slot=A panic=10\s*$'"
check "slot B directory exists" bash -c "mdir -i '$IMAGES/boot.vfat' ::b"
check "rauc on the device" rootfs_has /usr/bin/rauc
check "rauc config: custom bootloader, run-time mount prefix" bash -c "unsquashfs -cat '$IMAGES/rootfs.squashfs' etc/rauc/system.conf > /tmp/rauc-system.conf && grep -qx 'bootloader=custom' /tmp/rauc-system.conf && grep -qx 'mountprefix=/run/rauc' /tmp/rauc-system.conf && grep -qx 'compatible=loopi-rpi0w' /tmp/rauc-system.conf"
check "keyring is the OTA CA" bash -c "unsquashfs -cat '$IMAGES/rootfs.squashfs' etc/rauc/keyring.pem | cmp -s - '${LOOPI_RAUC_KEYS:-/keys}/ca.cert.pem'"
check "no private key in the rootfs" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' > /tmp/rootfs.list && ! grep -q 'key\.pem\$' /tmp/rootfs.list"
check "boot control and post-install handler" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' > /tmp/rootfs.list && grep -qx 'squashfs-root/usr/libexec/loopi/loopi-boot' /tmp/rootfs.list && grep -qx 'squashfs-root/usr/libexec/loopi/rauc-post-install' /tmp/rootfs.list"
check "trial guard timer enabled" rootfs_has /etc/systemd/system/timers.target.wants/loopi-tryboot-guard.timer
check "slot boot files staged in the rootfs" bash -c "unsquashfs -l '$IMAGES/rootfs.squashfs' > /tmp/rootfs.list && for f in zImage bcm2708-rpi-zero-w.dtb cmdline.txt loopi-config.txt overlays/README overlays/dwc2.dtbo; do grep -qx \"squashfs-root/usr/share/loopi/boot/\$f\" /tmp/rootfs.list || exit 1; done"
NAME="loopi-$variant-${LOOPI_OS_VERSION}"
check "bundle verifies against the CA" "$IMAGES/../host/bin/rauc" info --keyring="${LOOPI_RAUC_KEYS:-/keys}/ca.cert.pem" "$IMAGES/$NAME.raucb"
check "bundle description matches the bundle" python3 -c "
import hashlib, json, os, sys
d = json.load(open('$IMAGES/$NAME.json'))
b = '$IMAGES/' + d['bundle']
assert d['bundle'] == '$NAME.raucb' and d['variant'] == '$variant' and d['version'] == '${LOOPI_OS_VERSION}'
assert d['size'] == os.path.getsize(b) and d['sha256'] == hashlib.sha256(open(b, 'rb').read()).hexdigest()
assert len(d['commit']) == 40"
if is_dev; then
  check "dev: template keeps the gadget overlay" bash -c "mtype -i '$IMAGES/boot.vfat' ::a/loopi-config.txt | grep -qx 'dtoverlay=dwc2,dr_mode=peripheral'"
else
  check "prod: no gadget overlay anywhere in boot config" bash -c "! mtype -i '$IMAGES/boot.vfat' ::config.txt | grep -q dwc2 && ! mtype -i '$IMAGES/boot.vfat' ::a/loopi-config.txt | grep -q dwc2"
fi

exit $fail
