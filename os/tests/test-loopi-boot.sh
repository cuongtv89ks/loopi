#!/usr/bin/env bash
# Host test of the device's A/B boot control (os/board/loopi/rootfs-overlay/usr/libexec/loopi/loopi-boot), run with
# dash to catch bashisms the device's BusyBox sh would reject. Usage: os/tests/test-loopi-boot.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../board/loopi/rootfs-overlay/usr/libexec/loopi/loopi-boot"
SHELL_UNDER_TEST="${SHELL_UNDER_TEST:-dash}"
fail=0
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "[ OK ] $name"; else echo "[FAIL] $name"; fail=1; fi; }

setup() {  # fresh fake system: config.txt booting slot $1 (or "legacy"), booted slot $2 (or none), trial flag $3
  T="$(mktemp -d)"
  mkdir -p "$T/boot" "$T/state" "$T/root/usr/share/loopi/boot/overlays"
  printf 'kernel=zImage\ndtparam=audio=off\n# For the USB microphone run loopi-usbmode host.\ndtoverlay=dwc2,dr_mode=peripheral\n' > "$T/root/usr/share/loopi/boot/loopi-config.txt"
  printf 'root=@ROOT@ rootfstype=squashfs ro rootwait quiet rauc.slot=@SLOT@ panic=10\n' > "$T/root/usr/share/loopi/boot/cmdline.txt"
  echo kernel > "$T/root/usr/share/loopi/boot/zImage"; echo dtb > "$T/root/usr/share/loopi/boot/bcm2708-rpi-zero-w.dtb"
  echo overlay > "$T/root/usr/share/loopi/boot/overlays/dwc2.dtbo"
  case "$1" in
    legacy) printf 'kernel=zImage\n# dtoverlay=dwc2,dr_mode=peripheral\n' > "$T/boot/config.txt"
            echo old > "$T/boot/zImage"; echo old > "$T/boot/cmdline.txt"; mkdir "$T/boot/overlays" ;;
    a|b)    printf 'kernel=zImage\n# dtoverlay=dwc2,dr_mode=peripheral\n# Loopi slot\n[all]\nos_prefix=%s/\n' "$1" > "$T/boot/config.txt" ;;
  esac
  if [ "$2" = none ]; then echo 'root=/dev/mmcblk0p2 quiet' > "$T/cmdline"; else echo "root=/dev/x quiet rauc.slot=$2 panic=10" > "$T/cmdline"; fi
  if [ "$3" = 1 ]; then printf '\000\000\000\001' > "$T/flag"; else printf '\000\000\000\000' > "$T/flag"; fi
}
lb() {
  LOOPI_BOOT_DIR="$T/boot" LOOPI_STATE_DIR="$T/state" LOOPI_CMDLINE="$T/cmdline" LOOPI_TRYBOOT_FLAG="$T/flag" \
    LOOPI_REMOUNT=0 LOOPI_REBOOT_CMD="touch $T/rebooted" $SHELL_UNDER_TEST "$SCRIPT" "$@"  # unquoted: may be "busybox sh"
}
out_is() { [ "$(lb "${@:2}")" = "$1" ]; }

setup legacy none 0
check "legacy layout (no os_prefix) is slot A" out_is A get-primary
check "default state: primary good" out_is good get-state A
check "default state: other bad" out_is bad get-state B
check "installs slot B's boot files from a slot root" lb install-files B "$T/root"
check "slot B kernel copied" test -f "$T/boot/b/zImage"
check "slot B overlays copied" test -f "$T/boot/b/overlays/dwc2.dtbo"
check "slot B cmdline names p3 and rauc.slot=B" grep -qx 'root=/dev/mmcblk0p3 rootfstype=squashfs ro rootwait quiet rauc.slot=B panic=10' "$T/boot/b/cmdline.txt"
check "tryboot.txt boots b/" grep -qx 'os_prefix=b/' "$T/boot/tryboot.txt"
check "tryboot.txt has one os_prefix" test "$(grep -c '^os_prefix=' "$T/boot/tryboot.txt")" = 1
check "USB mode kept: gadget was off, stays off" grep -qx '# dtoverlay=dwc2,dr_mode=peripheral' "$T/boot/tryboot.txt"
check "config.txt untouched by an install" grep -qx 'kernel=zImage' "$T/boot/config.txt"
check "config.txt still has no os_prefix" bash -c "! grep -q '^os_prefix=' '$T/boot/config.txt'"
check "nothing half-copied left" test ! -e "$T/boot/b.new"

# the trial boots slot B; the agent confirms it
echo "root=/dev/mmcblk0p3 rauc.slot=B panic=10" > "$T/cmdline"; printf '\000\000\000\001' > "$T/flag"
check "trial pending while B runs unconfirmed" lb trial-pending
check "guard reboots an unconfirmed trial" bash -c "$(declare -f lb); T='$T' SCRIPT='$SCRIPT' SHELL_UNDER_TEST='$SHELL_UNDER_TEST' lb guard && test -e '$T/rebooted'"
rm -f "$T/rebooted"
check "confirming B promotes tryboot.txt" lb set-state B good
check "config.txt now boots b/" grep -qx 'os_prefix=b/' "$T/boot/config.txt"
check "tryboot.txt consumed" test ! -e "$T/boot/tryboot.txt"
check "B recorded good" out_is good get-state B
check "primary is B" out_is B get-primary
check "no trial pending once confirmed" bash -c "! (T='$T'; $(declare -f lb); T='$T' SCRIPT='$SCRIPT' SHELL_UNDER_TEST='$SHELL_UNDER_TEST' lb trial-pending)"
check "guard leaves a confirmed slot alone" bash -c "$(declare -f lb); T='$T' SCRIPT='$SCRIPT' SHELL_UNDER_TEST='$SHELL_UNDER_TEST' lb guard && test ! -e '$T/rebooted'"
check "legacy root files kept while slot A has no files yet" test -f "$T/boot/zImage"

# next update: slot A, from a running B
printf '\000\000\000\000' > "$T/flag"
check "refuses to overwrite the running slot's files" bash -c "! (T='$T'; $(declare -f lb); T='$T' SCRIPT='$SCRIPT' SHELL_UNDER_TEST='$SHELL_UNDER_TEST' lb install-files B '$T/root')"
check "set-state A bad before writing it" lb set-state A bad
check "installs slot A" lb install-files A "$T/root"
check "set-primary A writes tryboot.txt for a/" bash -c "$(declare -f lb); T='$T' SCRIPT='$SCRIPT' SHELL_UNDER_TEST='$SHELL_UNDER_TEST' lb set-primary A && grep -qx os_prefix=a/ '$T/boot/tryboot.txt'"
echo "root=/dev/mmcblk0p2 rauc.slot=A panic=10" > "$T/cmdline"; printf '\000\000\000\001' > "$T/flag"
check "confirming A promotes it" lb set-state A good
check "config.txt boots a/" grep -qx 'os_prefix=a/' "$T/boot/config.txt"
check "legacy root kernel removed once both slots have files" test ! -e "$T/boot/zImage"
check "legacy root cmdline removed" test ! -e "$T/boot/cmdline.txt"
check "legacy root overlays removed" test ! -e "$T/boot/overlays"

# Review Focus: never write config.txt from nothing, never promote a slot that is not booted
setup b B 1
cp "$T/boot/config.txt" "$T/before"
echo "root=/dev/x rauc.slot=A" > "$T/cmdline"
check "confirm without tryboot.txt fails" bash -c "! (T='$T'; $(declare -f lb); T='$T' SCRIPT='$SCRIPT' SHELL_UNDER_TEST='$SHELL_UNDER_TEST' lb set-state A good)"
check "config.txt unchanged after the refused confirm" cmp -s "$T/before" "$T/boot/config.txt"
setup a A 0
lb install-files B "$T/root" >/dev/null 2>&1
cp "$T/boot/config.txt" "$T/before"
check "good for a slot that is not booted only records it" lb set-state B good
check "config.txt unchanged (B not booted)" cmp -s "$T/before" "$T/boot/config.txt"
check "tryboot.txt for B still there" grep -qx os_prefix=b/ "$T/boot/tryboot.txt"
check "bad for the slot under trial drops its tryboot.txt" lb set-state B bad
check "tryboot.txt gone" test ! -e "$T/boot/tryboot.txt"
check "set-primary of the primary drops a stale tryboot.txt" bash -c "$(declare -f lb); T='$T' SCRIPT='$SCRIPT' SHELL_UNDER_TEST='$SHELL_UNDER_TEST' lb set-primary A && test ! -e '$T/boot/tryboot.txt'"

# USB mode: an active gadget stays active; a template without it (prod) never gets it
setup a A 0
sed -i 's/^# dtoverlay=dwc2/dtoverlay=dwc2/' "$T/boot/config.txt"
lb install-files B "$T/root" >/dev/null 2>&1
check "USB mode kept: gadget on stays on" grep -qx 'dtoverlay=dwc2,dr_mode=peripheral' "$T/boot/tryboot.txt"
grep -v dwc2 "$T/root/usr/share/loopi/boot/loopi-config.txt" > "$T/prod" && mv "$T/prod" "$T/root/usr/share/loopi/boot/loopi-config.txt"
lb install-files B "$T/root" >/dev/null 2>&1
check "prod template never gets the gadget overlay" bash -c "! grep -q dwc2 '$T/boot/tryboot.txt'"
check "re-rendering keeps one slot block" test "$(grep -c '^# Loopi slot$' "$T/boot/tryboot.txt")" = 1

check "unknown slot refused" bash -c "! (T='$T'; $(declare -f lb); T='$T' SCRIPT='$SCRIPT' SHELL_UNDER_TEST='$SHELL_UNDER_TEST' lb get-state C)"
check "unknown command refused" bash -c "! (T='$T'; $(declare -f lb); T='$T' SCRIPT='$SCRIPT' SHELL_UNDER_TEST='$SHELL_UNDER_TEST' lb frobnicate)"
check "get-current reads rauc.slot" out_is A get-current
exit "$fail"
