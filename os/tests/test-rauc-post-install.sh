#!/usr/bin/env bash
# Host test of RAUC's post-install handler (os/board/loopi/rootfs-overlay/usr/libexec/loopi/rauc-post-install) against a
# fake `mount`/`umount` and the repo's own loopi-boot, run with dash (SHELL_UNDER_TEST overrides, e.g. "busybox sh") to
# catch bashisms the device's BusyBox sh would reject. Usage: os/tests/test-rauc-post-install.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LIBEXEC="$HERE/../board/loopi/rootfs-overlay/usr/libexec/loopi"
SCRIPT="$LIBEXEC/rauc-post-install"
BOOT_SCRIPT="$LIBEXEC/loopi-boot"
SHELL_UNDER_TEST="${SHELL_UNDER_TEST:-dash}"
fail=0
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "[ OK ] $name"; else echo "[FAIL] $name"; fail=1; fi; }

# A fresh fake system: config.txt boots slot A, A runs, the new rootfs (what a mount of the slot device shows) is staged
# under $T/root. The fake mount copies it into the mount point and the fake umount empties the mount point again, so the
# handler's own rmdir only works when it unmounted.
setup() {
  T="$(mktemp -d)"
  mkdir -p "$T/boot" "$T/state" "$T/run" "$T/bin" "$T/root/usr/share/loopi/boot/overlays"
  R="$T/root/usr/share/loopi/boot"
  printf 'kernel=zImage\ndtparam=audio=off\n# For the USB microphone run loopi-usbmode host.\ndtoverlay=dwc2,dr_mode=peripheral\n' > "$R/loopi-config.txt"
  printf 'root=@ROOT@ rootfstype=squashfs ro rootwait quiet rauc.slot=@SLOT@ panic=10\n' > "$R/cmdline.txt"
  echo kernel > "$R/zImage"; echo dtb > "$R/bcm2708-rpi-zero-w.dtb"
  echo overlay > "$R/overlays/dwc2.dtbo"; echo readme > "$R/overlays/README"
  printf 'kernel=zImage\n# dtoverlay=dwc2,dr_mode=peripheral\n# Loopi slot\n[all]\nos_prefix=a/\n' > "$T/boot/config.txt"
  echo "root=/dev/mmcblk0p2 quiet rauc.slot=A panic=10" > "$T/cmdline"
  printf '\000\000\000\000' > "$T/flag"
  : > "$T/mock.log"
  cat > "$T/bin/mount" <<'EOF'
#!/bin/sh
echo "mount $*" >> "$MOCK_LOG"
[ "$#" = 6 ] && [ "$1 $2 $3 $4" = "-t squashfs -o ro" ] && [ "$5" = /dev/fake ] || exit 32
[ "${MOCK_MOUNT_FAIL:-0}" != 1 ] || exit 32
cp -R "$MOCK_ROOT/." "$6/"
EOF
  cat > "$T/bin/umount" <<'EOF'
#!/bin/sh
echo "umount $*" >> "$MOCK_LOG"
find "$1" -mindepth 1 -delete
EOF
  # The repo's loopi-boot under the shell being tested (the real one is started by its #!/bin/sh line).
  printf '#!/bin/sh\nexec %s "%s" "$@"\n' "$SHELL_UNDER_TEST" "$BOOT_SCRIPT" > "$T/bin/loopi-boot"
  chmod 755 "$T/bin/mount" "$T/bin/umount" "$T/bin/loopi-boot"
}
# The handler is sourced after two shell functions that stand for mount and umount: a BusyBox sh built with standalone
# applets (Ubuntu's) runs its own applet before it looks at PATH, but always honours a function.
HOOKS='mount() { "$MOCK_BIN/mount" "$@"; }; umount() { "$MOCK_BIN/umount" "$@"; };'
# pi <extra env assignments...>: the handler as RAUC runs it. Output and status go to $OUT and $RC.
pi() {
  OUT="$(env -u RAUC_TARGET_SLOTS PATH="$T/bin:$PATH" MOCK_BIN="$T/bin" MOCK_LOG="$T/mock.log" MOCK_ROOT="$T/root" \
    LOOPI_BOOT_BIN="$T/bin/loopi-boot" LOOPI_RUN_DIR="$T/run" \
    LOOPI_BOOT_DIR="$T/boot" LOOPI_STATE_DIR="$T/state" LOOPI_CMDLINE="$T/cmdline" LOOPI_TRYBOOT_FLAG="$T/flag" \
    LOOPI_REMOUNT=0 "$@" $SHELL_UNDER_TEST -c "$HOOKS"' . "$0"' "$SCRIPT" 2>&1)"; RC=$?   # unquoted: may be "busybox sh"
}
says() { case "$OUT" in *"$1"*) return 0 ;; esac; return 1; }
logged() { grep -qF -- "$1" "$T/mock.log"; }
nothing_written() { [ ! -e "$T/boot/b" ] && [ ! -e "$T/boot/b.new" ] && [ ! -e "$T/boot/a.new" ] && [ ! -e "$T/boot/tryboot.txt" ]; }
mount_point_gone() { [ -z "$(ls -A "$T/run")" ]; }
slot_b=(RAUC_TARGET_SLOTS=1 RAUC_SLOT_DEVICE_1=/dev/fake RAUC_SLOT_BOOTNAME_1=B)
# lb_env <loopi-boot args...>: the repo's loopi-boot on the fake system, as RAUC's custom bootloader backend calls it.
lb_env() {
  env LOOPI_BOOT_DIR="$T/boot" LOOPI_STATE_DIR="$T/state" LOOPI_CMDLINE="$T/cmdline" LOOPI_TRYBOOT_FLAG="$T/flag" \
    LOOPI_REMOUNT=0 "$T/bin/loopi-boot" "$@"
}

# the normal case: slot B was written, its boot files and tryboot.txt are installed
setup
pi "${slot_b[@]}"
check "slot B: succeeds" test "$RC" = 0
check "slot B: the device is mounted read-only as squashfs" logged "mount -t squashfs -o ro /dev/fake $T/run/loopi-slot."
check "slot B: unmounted again" logged "umount $T/run/loopi-slot."
check "slot B: mount point removed" mount_point_gone
check "slot B: kernel copied to b/" test -f "$T/boot/b/zImage"
check "slot B: overlays/README copied to b/" test -f "$T/boot/b/overlays/README"
check "slot B: cmdline names p3 and rauc.slot=B" grep -qx 'root=/dev/mmcblk0p3 rootfstype=squashfs ro rootwait quiet rauc.slot=B panic=10' "$T/boot/b/cmdline.txt"
check "slot B: tryboot.txt boots b/" grep -qx 'os_prefix=b/' "$T/boot/tryboot.txt"
check "slot B: config.txt still boots a/" grep -qx 'os_prefix=a/' "$T/boot/config.txt"

# RAUC 1.15's real order: the target is marked bad and ACTIVATED (set-primary) before the post-install handler has put its
# boot files on the boot partition, so set-primary sees a slot without files and must not fail the install
setup
OUT="$(lb_env set-state B bad 2>&1)"; RC=$?
check "RAUC order: set-state B bad succeeds" test "$RC" = 0
OUT="$(lb_env set-primary B 2>&1)"; RC=$?
check "RAUC order: set-primary B before the handler (no b/ yet) succeeds" test "$RC" = 0
check "RAUC order: set-primary says install-files writes tryboot.txt" says "install-files writes it"
check "RAUC order: no tryboot.txt after set-primary alone" test ! -e "$T/boot/tryboot.txt"
pi "${slot_b[@]}"
check "RAUC order: the handler then succeeds" test "$RC" = 0
check "RAUC order: tryboot.txt exists afterwards" test -f "$T/boot/tryboot.txt"
check "RAUC order: tryboot.txt boots b/" grep -qx 'os_prefix=b/' "$T/boot/tryboot.txt"
check "RAUC order: b/ has its kernel" test -f "$T/boot/b/zImage"
check "RAUC order: config.txt still boots a/" grep -qx 'os_prefix=a/' "$T/boot/config.txt"
check "RAUC order: mount point removed" mount_point_gone

# a LATER install into a slot that already has its (older) boot files, in RAUC's order: set-primary writes tryboot.txt from the
# old files, the handler then replaces them and tryboot.txt must come from the new template
setup
echo '# template v1' >> "$R/loopi-config.txt"
pi "${slot_b[@]}"
check "later install: the first install puts v1 in b/" grep -qx '# template v1' "$T/boot/b/loopi-config.txt"
sed -i 's/^# template v1$/# template v2/' "$R/loopi-config.txt"; echo kernel2 > "$R/zImage"
OUT="$(lb_env set-state B bad 2>&1)"; RC=$?
check "later install, RAUC order: set-state B bad succeeds" test "$RC" = 0
OUT="$(lb_env set-primary B 2>&1)"; RC=$?
check "later install, RAUC order: set-primary B (b/ has v1) succeeds" test "$RC" = 0
check "later install, RAUC order: set-primary wrote tryboot.txt from the v1 template" grep -qx '# template v1' "$T/boot/tryboot.txt"
pi "${slot_b[@]}"
check "later install, RAUC order: the handler then succeeds" test "$RC" = 0
check "later install, RAUC order: b/ has the v2 kernel" grep -qx kernel2 "$T/boot/b/zImage"
check "later install, RAUC order: tryboot.txt is rendered from the v2 template" grep -qx '# template v2' "$T/boot/tryboot.txt"
check "later install, RAUC order: no v1 line left in tryboot.txt" bash -c "! grep -q 'template v1' '$T/boot/tryboot.txt'"
check "later install, RAUC order: tryboot.txt boots b/" grep -qx 'os_prefix=b/' "$T/boot/tryboot.txt"
check "later install, RAUC order: config.txt still boots a/" grep -qx 'os_prefix=a/' "$T/boot/config.txt"
check "later install, RAUC order: mount point removed" mount_point_gone

# a target list with a slot that has no boot name (not a rootfs slot) is skipped, the rootfs slot is still installed
setup
pi RAUC_TARGET_SLOTS="1 2" RAUC_SLOT_DEVICE_1=/dev/other RAUC_SLOT_DEVICE_2=/dev/fake RAUC_SLOT_BOOTNAME_2=B
check "a slot without a boot name is skipped, the next one installed" test "$RC" = 0 -a -f "$T/boot/b/zImage"
check "the slot without a boot name is never mounted" bash -c "! grep -q /dev/other '$T/mock.log'"
check "tryboot.txt boots b/ for the second slot" grep -qx 'os_prefix=b/' "$T/boot/tryboot.txt"

# no slot handled: that is a failure, never a silent success
setup
pi RAUC_TARGET_SLOTS=1 RAUC_SLOT_DEVICE_1=/dev/fake
check "no boot name: fails" test "$RC" != 0
check "no boot name: says nothing was installed" says "no target slot had a boot name; nothing installed"
check "no boot name: nothing mounted" test ! -s "$T/mock.log"
check "no boot name: nothing written" nothing_written
setup
pi RAUC_TARGET_SLOTS=1 RAUC_SLOT_DEVICE_1=/dev/fake RAUC_SLOT_BOOTNAME_1=
check "an empty boot name: fails, nothing written" bash -c "test '$RC' != 0 && test ! -e '$T/boot/b' -a ! -e '$T/boot/tryboot.txt'"
setup
pi
check "no target slots at all: fails" test "$RC" != 0
check "no target slots at all: says nothing was installed" says "nothing installed"
check "no target slots at all: nothing written" nothing_written
setup
pi RAUC_TARGET_SLOTS=
check "an empty target list: fails" test "$RC" != 0

# a slot root that is not complete is refused, nothing is written, and the mount is undone
setup
rm "$T/root/usr/share/loopi/boot/overlays/README"
pi "${slot_b[@]}"
check "incomplete slot root: fails" test "$RC" != 0
check "incomplete slot root: says what is missing" says "overlays/README is missing"
check "incomplete slot root: nothing written" nothing_written
check "incomplete slot root: unmounted again" logged "umount $T/run/loopi-slot."
check "incomplete slot root: mount point removed" mount_point_gone
setup
rm "$T/root/usr/share/loopi/boot/zImage"
pi "${slot_b[@]}"
check "slot root without a kernel: fails, nothing written" bash -c "test '$RC' != 0 && test ! -e '$T/boot/b' -a ! -e '$T/boot/tryboot.txt'"

# the target is the slot that is running: its boot files stay as they are
setup
echo "root=/dev/mmcblk0p3 quiet rauc.slot=B panic=10" > "$T/cmdline"
pi "${slot_b[@]}"
check "target is the booted slot: fails" test "$RC" != 0
check "target is the booted slot: says it is running" says "is running"
check "target is the booted slot: nothing written" nothing_written
check "target is the booted slot: unmounted again" logged "umount $T/run/loopi-slot."

# the mount fails: failure, nothing written
setup
pi "${slot_b[@]}" MOCK_MOUNT_FAIL=1
check "mount fails: fails" test "$RC" != 0
check "mount fails: nothing written" nothing_written
check "mount fails: mount point removed" mount_point_gone
check "mount fails: says which device" says "could not mount /dev/fake"

exit "$fail"
