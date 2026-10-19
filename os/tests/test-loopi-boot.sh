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
  echo readme > "$T/root/usr/share/loopi/boot/overlays/README"
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
refused() { ! lb "$@"; }                                   # the command must exit non-zero
msg_has() { local m; m="$(lb "${@:2}" 2>&1)"; case "$m" in *"$1"*) return 0 ;; esac; return 1; }   # its output must contain $1
out_says() { case "$OUT" in *"$1"*) return 0 ;; esac; return 1; }   # the output captured in $OUT must contain $1
state_last_is() { [ "$(tail -n 1 "$T/state/boot-state")" = "$1" ]; }
guard_reboots() { rm -f "$T/rebooted"; lb guard && test -e "$T/rebooted"; }
guard_idles() { rm -f "$T/rebooted"; lb guard && test ! -e "$T/rebooted"; }

setup legacy none 0
check "legacy layout (no os_prefix) is slot A" out_is A get-primary
check "default state: primary good" out_is good get-state A
check "default state: other bad" out_is bad get-state B
check "installs slot B's boot files from a slot root" lb install-files B "$T/root"
check "slot B kernel copied" test -f "$T/boot/b/zImage"
check "slot B overlays copied" test -f "$T/boot/b/overlays/dwc2.dtbo"
check "slot B overlays/README copied (the firmware loads b/overlays only with it)" test -f "$T/boot/b/overlays/README"
check "slot B cmdline names p3 and rauc.slot=B" grep -qx 'root=/dev/mmcblk0p3 rootfstype=squashfs ro rootwait quiet rauc.slot=B panic=10' "$T/boot/b/cmdline.txt"
check "tryboot.txt boots b/" grep -qx 'os_prefix=b/' "$T/boot/tryboot.txt"
check "tryboot.txt has one os_prefix" test "$(grep -c '^os_prefix=' "$T/boot/tryboot.txt")" = 1
check "USB mode kept: gadget was off, stays off" grep -qx '# dtoverlay=dwc2,dr_mode=peripheral' "$T/boot/tryboot.txt"
check "config.txt untouched by an install" grep -qx 'kernel=zImage' "$T/boot/config.txt"
check "config.txt still has no os_prefix" bash -c "! grep -q '^os_prefix=' '$T/boot/config.txt'"
check "nothing half-copied left" test ! -e "$T/boot/b.new"
check "no half-written tryboot.txt left" test ! -e "$T/boot/tryboot.txt.new"

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

# next update: slot A, from a running B, in RAUC's order (set-state bad, set-primary, then the post-install handler)
printf '\000\000\000\000' > "$T/flag"
check "refuses to overwrite the running slot's files" bash -c "! (T='$T'; $(declare -f lb); T='$T' SCRIPT='$SCRIPT' SHELL_UNDER_TEST='$SHELL_UNDER_TEST' lb install-files B '$T/root')"
check "set-state A bad before writing it" lb set-state A bad
check "set-primary A (no a/ yet) writes no tryboot.txt" bash -c "$(declare -f lb); T='$T' SCRIPT='$SCRIPT' SHELL_UNDER_TEST='$SHELL_UNDER_TEST' lb set-primary A && test ! -e '$T/boot/tryboot.txt'"
check "installs slot A" lb install-files A "$T/root"
check "install-files A writes tryboot.txt for a/" grep -qx os_prefix=a/ "$T/boot/tryboot.txt"
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

# Fix round 1 (D26: rollback must stay possible)
# 1. The fallback slot of an unconfirmed trial (the one config.txt boots) is never rewritten
setup legacy none 0
lb install-files A "$T/root" >/dev/null 2>&1 && mv "$T/boot/tryboot.txt" "$T/boot/config.txt"   # the image-build layout: config.txt boots a/
lb install-files B "$T/root" >/dev/null 2>&1                                                    # tryboot.txt boots b/
echo "root=/dev/mmcblk0p3 rauc.slot=B panic=10" > "$T/cmdline"; printf '\000\000\000\001' > "$T/flag"
cp "$T/boot/config.txt" "$T/config.before"; cp "$T/boot/tryboot.txt" "$T/tryboot.before"; cp -r "$T/boot/a" "$T/a.before"
check "unconfirmed trial of B: set-state A bad is refused" refused set-state A bad
check "unconfirmed trial of B: install-files A is refused" refused install-files A "$T/root"
check "the refusal says A is the fallback of a trial" msg_has "fallback" install-files A "$T/root"
check "config.txt byte-identical after the refusals" cmp -s "$T/config.before" "$T/boot/config.txt"
check "tryboot.txt (b/) byte-identical after the refusals" cmp -s "$T/tryboot.before" "$T/boot/tryboot.txt"
check "a/ byte-identical after the refusals" diff -rq "$T/a.before" "$T/boot/a"
check "A not recorded bad by the refusal" out_is good get-state A
check "the trial can still be confirmed afterwards" lb set-state B good
check "and then boots b/" grep -qx 'os_prefix=b/' "$T/boot/config.txt"
setup a none 0; : > "$T/cmdline"      # no rauc.slot= (image build with LOOPI_CMDLINE=/dev/null, migration on the old system)
check "empty cmdline: set-state of the primary bad is allowed" lb set-state A bad
check "empty cmdline: install-files of the primary is allowed" lb install-files A "$T/root"
setup a A 0
check "booted from config.txt (no trial): set-state of the other slot bad is allowed" lb set-state B bad

# 2. A partially rendered file is never renamed into place
setup a A 0
lb install-files B "$T/root" >/dev/null 2>&1
cp "$T/boot/tryboot.txt" "$T/tryboot.before"
mkdir "$T/boot/tryboot.txt.new"      # the temporary sibling cannot be written
check "tryboot.txt that cannot be rendered is refused" refused set-primary B
check "previous tryboot.txt kept after the failed render" cmp -s "$T/tryboot.before" "$T/boot/tryboot.txt"
rmdir "$T/boot/tryboot.txt.new"
chmod 000 "$T/boot/b/loopi-config.txt"
if [ -r "$T/boot/b/loopi-config.txt" ]; then
  echo "[SKIP] unreadable template (running as root: chmod 000 does not stop reads)"
else
  check "unreadable template: set-primary is refused" refused set-primary B
  check "unreadable template: previous tryboot.txt kept" cmp -s "$T/tryboot.before" "$T/boot/tryboot.txt"
  rm -f "$T/boot/tryboot.txt"
  check "unreadable template, no tryboot.txt yet: still refused" refused set-primary B
  check "unreadable template: no tryboot.txt made from nothing" test ! -e "$T/boot/tryboot.txt"
  check "unreadable template: no half-written tryboot.txt.new left" test ! -e "$T/boot/tryboot.txt.new"
fi
chmod 644 "$T/boot/b/loopi-config.txt"
# the same for boot-state, and a trial is not confirmed when its state cannot be recorded
setup a A 0
lb set-state B bad >/dev/null 2>&1
cp "$T/state/boot-state" "$T/state.before"
mkdir "$T/state/boot-state.new"
check "boot-state that cannot be written is refused" refused set-state A bad
check "previous boot-state kept after the failed write" cmp -s "$T/state.before" "$T/state/boot-state"
rmdir "$T/state/boot-state.new"
check "boot-state still lists B bad last" state_last_is "B bad"
lb install-files B "$T/root" >/dev/null 2>&1
echo "root=/dev/mmcblk0p3 rauc.slot=B panic=10" > "$T/cmdline"; printf '\000\000\000\001' > "$T/flag"
cp "$T/boot/config.txt" "$T/config.before"
mkdir "$T/state/boot-state.new"
check "confirm with an unwritable boot-state is refused" refused set-state B good
check "config.txt untouched when the state could not be recorded" cmp -s "$T/config.before" "$T/boot/config.txt"
check "tryboot.txt kept so the confirmation can be retried" grep -qx 'os_prefix=b/' "$T/boot/tryboot.txt"
rmdir "$T/state/boot-state.new"

# 3. install-files needs overlays/README in the slot root
setup a A 0
cp -r "$T/root" "$T/root-no-readme"; rm "$T/root-no-readme/usr/share/loopi/boot/overlays/README"
check "a slot root without overlays/README is refused" refused install-files B "$T/root-no-readme"
check "nothing written for it" test ! -e "$T/boot/b" -a ! -e "$T/boot/b.new" -a ! -e "$T/boot/tryboot.txt"

# 4. The guard reboots an unconfirmed trial even when /boot did not mount (no config.txt)
setup a A 0
lb install-files B "$T/root" >/dev/null 2>&1
echo "root=/dev/mmcblk0p3 rauc.slot=B panic=10" > "$T/cmdline"; printf '\000\000\000\001' > "$T/flag"
rm -f "$T/boot/config.txt"
check "trial without config.txt counts as pending" lb trial-pending
check "guard reboots a trial that has no config.txt" guard_reboots
printf '\000\000\000\000' > "$T/flag"
check "guard leaves a boot that is not a trial alone (no config.txt)" guard_idles
# Final review, Minor 1, and the P4a device run (RAUC 1.15 activates the target BEFORE its post-install handler has copied
# the boot files): set-primary of a slot whose boot files are not all there succeeds but writes no tryboot.txt, so that
# tryboot.txt never points at a slot without a kernel; install-files writes it once the files are there.
setup a A 0
cp "$T/boot/config.txt" "$T/config.before"
check "set-primary of a slot with no boot directory succeeds" lb set-primary B
check "the note says install-files writes tryboot.txt" msg_has "install-files" set-primary B
check "the note says what is missing" msg_has "missing: b/zImage" set-primary B
check "nothing written for it" test ! -e "$T/boot/tryboot.txt" -a ! -e "$T/boot/tryboot.txt.new" -a ! -e "$T/boot/b"
check "config.txt byte-identical" cmp -s "$T/config.before" "$T/boot/config.txt"
for f in zImage bcm2708-rpi-zero-w.dtb cmdline.txt overlays/README; do
  setup a A 0
  lb install-files B "$T/root" >/dev/null 2>&1
  rm "$T/boot/b/$f"
  check "b/$f missing: set-primary B succeeds" lb set-primary B
  check "b/$f missing: the stale tryboot.txt is removed" test ! -e "$T/boot/tryboot.txt"
  rm -f "$T/boot/tryboot.txt"
  check "b/$f missing, no tryboot.txt yet: set-primary B succeeds" lb set-primary B
  check "b/$f missing: no tryboot.txt made from nothing" test ! -e "$T/boot/tryboot.txt" -a ! -e "$T/boot/tryboot.txt.new"
done
setup a A 0
lb install-files B "$T/root" >/dev/null 2>&1
rm -f "$T/boot/tryboot.txt"
check "set-primary B with all of b/'s files writes tryboot.txt" lb set-primary B
check "set-primary B: tryboot.txt boots b/" grep -qx 'os_prefix=b/' "$T/boot/tryboot.txt"
check "set-primary B: still no tryboot.txt.new left" test ! -e "$T/boot/tryboot.txt.new"
# a stale tryboot.txt of the other slot goes away when set-primary hits a slot without files
setup a A 0
printf 'kernel=zImage\n# Loopi slot\n[all]\nos_prefix=a/\n' > "$T/boot/tryboot.txt"
check "stale tryboot.txt (a/), slot B has no files: set-primary B succeeds" lb set-primary B
check "stale tryboot.txt (a/) removed" test ! -e "$T/boot/tryboot.txt"
setup b B 0
printf 'kernel=zImage\n# Loopi slot\n[all]\nos_prefix=b/\n' > "$T/boot/tryboot.txt"
check "stale tryboot.txt (b/), slot A has no files: set-primary A succeeds" lb set-primary A
check "stale tryboot.txt (b/) removed" test ! -e "$T/boot/tryboot.txt"
setup a A 0
lb install-files B "$T/root" >/dev/null 2>&1
check "set-primary of the primary needs no boot files of its own (no a/ here) and drops tryboot.txt" bash -c "$(declare -f lb); T='$T' SCRIPT='$SCRIPT' SHELL_UNDER_TEST='$SHELL_UNDER_TEST' lb set-primary A && test ! -e '$T/boot/tryboot.txt'"

# P4a device run: RAUC 1.15's real order from a booted, confirmed slot B that has no /boot/a/ yet:
#   set-state A bad -> (write the image) -> set-primary A (activate) -> post-install: install-files A <root>
setup b B 0
mkdir "$T/boot/b"; cp -r "$T/root/usr/share/loopi/boot/." "$T/boot/b/"
cp "$T/boot/config.txt" "$T/config.before"
check "RAUC order: set-state A bad" lb set-state A bad
check "RAUC order: set-primary A (a/ not there yet) succeeds" lb set-primary A
check "RAUC order: no tryboot.txt yet" test ! -e "$T/boot/tryboot.txt"
check "RAUC order: a/ not invented by set-primary" test ! -e "$T/boot/a"
check "RAUC order: config.txt byte-identical after set-primary" cmp -s "$T/config.before" "$T/boot/config.txt"
check "RAUC order: install-files A (the post-install)" lb install-files A "$T/root"
check "RAUC order: a/ created with its kernel" test -f "$T/boot/a/zImage" -a -f "$T/boot/a/overlays/README"
check "RAUC order: tryboot.txt says os_prefix=a/" grep -qx 'os_prefix=a/' "$T/boot/tryboot.txt"
check "RAUC order: tryboot.txt has one os_prefix" test "$(grep -c '^os_prefix=' "$T/boot/tryboot.txt")" = 1
check "RAUC order: config.txt still byte-identical (still boots b/)" cmp -s "$T/config.before" "$T/boot/config.txt"
check "RAUC order: A still recorded bad until confirmed" out_is bad get-state A
check "RAUC order: B stays the primary" out_is B get-primary
# the "0 tryboot" reboot boots A in trial; the agent confirms it
echo "root=/dev/mmcblk0p2 rauc.slot=A panic=10" > "$T/cmdline"; printf '\000\000\000\001' > "$T/flag"
check "RAUC order: trial pending while A runs unconfirmed" lb trial-pending
check "RAUC order: set-state A good promotes" lb set-state A good
check "RAUC order: config.txt now boots a/" grep -qx 'os_prefix=a/' "$T/boot/config.txt"
check "RAUC order: tryboot.txt consumed" test ! -e "$T/boot/tryboot.txt"
check "RAUC order: A recorded good" out_is good get-state A
check "RAUC order: primary is A" out_is A get-primary
check "RAUC order: no trial pending once confirmed" refused trial-pending
# the post-install fails (a slot root without overlays/README): no tryboot.txt, so the install is reported as failed and the
# next reboot stays on B
setup b B 0
lb set-state A bad >/dev/null 2>&1
lb set-primary A >/dev/null 2>&1
cp -r "$T/root" "$T/root-no-readme"; rm "$T/root-no-readme/usr/share/loopi/boot/overlays/README"
check "RAUC order, post-install fails: install-files A is refused" refused install-files A "$T/root-no-readme"
check "RAUC order, post-install fails: no tryboot.txt, no a/" test ! -e "$T/boot/tryboot.txt" -a ! -e "$T/boot/a" -a ! -e "$T/boot/a.new"

# P4a invariant: tryboot.txt never names a slot without a complete kernel, also on a LATER install into a slot that already
# has (older) boot files. There set-primary writes tryboot.txt from the old files, and install-files then replaces them.
# v1 is what a/ holds, v2 the new bundle's root: its template carries a marker line and its kernel is different.
# lb_swap_fails <args>: loopi-boot as lb runs it, sourced after a function that makes the rename into the slot directory
# (a write error on the boot partition between removing the old files and putting the new ones) fail; any other mv works.
# A function and not a fake on PATH: a BusyBox sh built with standalone applets runs its own mv before it looks at PATH.
lb_swap_fails() {
  FAIL_MV_DEST="$T/boot/a" LOOPI_BOOT_DIR="$T/boot" LOOPI_STATE_DIR="$T/state" LOOPI_CMDLINE="$T/cmdline" \
    LOOPI_TRYBOOT_FLAG="$T/flag" LOOPI_REMOUNT=0 LOOPI_REBOOT_CMD="touch $T/rebooted" \
    $SHELL_UNDER_TEST -c 'mv() { for last in "$@"; do :; done; [ "$last" != "$FAIL_MV_DEST" ] || return 1; command mv "$@"; }; . "$0"' "$SCRIPT" "$@"
}
swap_refused() { ! lb_swap_fails "$@"; }
no_tryboot_for_a() { ! grep -qx 'os_prefix=a/' "$T/boot/tryboot.txt" 2> /dev/null; }
later_install_setup() {  # booted, confirmed B; a/ holds v1 from an earlier OTA; $T/root2 is the new bundle's root
  setup b B 0
  echo '# template v1' >> "$T/root/usr/share/loopi/boot/loopi-config.txt"
  lb install-files A "$T/root" >/dev/null 2>&1
  rm -f "$T/boot/tryboot.txt"
  cp -r "$T/root" "$T/root2"
  B2="$T/root2/usr/share/loopi/boot"
  sed -i 's/^# template v1$/# template v2/' "$B2/loopi-config.txt"; echo kernel2 > "$B2/zImage"
  cp "$T/boot/config.txt" "$T/config.before"
}
later_install_setup
check "later install: a/ holds v1" grep -qx kernel "$T/boot/a/zImage"
check "later install, RAUC order: set-state A bad" lb set-state A bad
check "later install, RAUC order: set-primary A (a/ has v1) succeeds" lb set-primary A
check "later install, RAUC order: set-primary wrote tryboot.txt for a/ from the v1 template" grep -qx '# template v1' "$T/boot/tryboot.txt"
check "later install, RAUC order: tryboot.txt boots a/" grep -qx 'os_prefix=a/' "$T/boot/tryboot.txt"
check "later install, RAUC order: install-files A with the v2 root (the post-install)" lb install-files A "$T/root2"
check "later install, RAUC order: a/ has the v2 kernel" grep -qx kernel2 "$T/boot/a/zImage"
check "later install, RAUC order: a/ has the v2 template" grep -qx '# template v2' "$T/boot/a/loopi-config.txt"
check "later install, RAUC order: tryboot.txt is rendered from the v2 template" grep -qx '# template v2' "$T/boot/tryboot.txt"
check "later install, RAUC order: no v1 line left in tryboot.txt" bash -c "! grep -q 'template v1' '$T/boot/tryboot.txt'"
check "later install, RAUC order: tryboot.txt boots a/" grep -qx 'os_prefix=a/' "$T/boot/tryboot.txt"
check "later install, RAUC order: tryboot.txt has one os_prefix" test "$(grep -c '^os_prefix=' "$T/boot/tryboot.txt")" = 1
check "later install, RAUC order: config.txt byte-identical (still boots b/)" cmp -s "$T/config.before" "$T/boot/config.txt"
check "later install, RAUC order: nothing half-copied left" test ! -e "$T/boot/a.new" -a ! -e "$T/boot/tryboot.txt.new"
# the swap fails after the old files are gone: the install is reported as failed and no tryboot.txt names the slot
later_install_setup
lb set-state A bad >/dev/null 2>&1
lb set-primary A >/dev/null 2>&1
check "later install, failing swap: tryboot.txt names a/ before the post-install" grep -qx 'os_prefix=a/' "$T/boot/tryboot.txt"
check "later install, failing swap: install-files A is refused" swap_refused install-files A "$T/root2"
check "later install, failing swap: a/ has no kernel (the old files are gone)" test ! -e "$T/boot/a/zImage"
check "later install, failing swap: no tryboot.txt names a/" no_tryboot_for_a
check "later install, failing swap: no tryboot.txt at all" test ! -e "$T/boot/tryboot.txt" -a ! -e "$T/boot/tryboot.txt.new"
check "later install, failing swap: config.txt byte-identical" cmp -s "$T/config.before" "$T/boot/config.txt"
check "later install, failing swap: a retry succeeds" lb install-files A "$T/root2"
check "later install, failing swap: the retry writes tryboot.txt from the v2 template" grep -qx '# template v2' "$T/boot/tryboot.txt"
check "later install, failing swap: the retry leaves a/ with the v2 kernel" grep -qx kernel2 "$T/boot/a/zImage"
# a tryboot.txt that names the other slot is not this slot's to remove
later_install_setup
printf 'kernel=zImage\n# Loopi slot\n[all]\nos_prefix=b/\n' > "$T/boot/tryboot.txt"
check "failing swap of a/ with a tryboot.txt that names b/: install-files A is refused" swap_refused install-files A "$T/root2"
check "failing swap of a/ with a tryboot.txt that names b/: that tryboot.txt is left alone" grep -qx 'os_prefix=b/' "$T/boot/tryboot.txt"

# Final review, Minor 2: a trial boot without rauc.slot= (the firmware fell back to the legacy root kernel) is unconfirmed
setup a none 1
lb install-files B "$T/root" >/dev/null 2>&1
check "trial flag set but no rauc.slot=: trial pending" lb trial-pending
check "trial flag set but no rauc.slot=: the guard reboots" guard_reboots
setup a none 0
lb install-files B "$T/root" >/dev/null 2>&1
check "no rauc.slot= and no trial flag: no trial pending" refused trial-pending
check "no rauc.slot= and no trial flag: the guard leaves it alone" guard_idles
setup legacy none 1
check "trial flag set, legacy layout, no rauc.slot=: the guard reboots" guard_reboots

# Final review, Minor 3: a legacy file that cannot be removed does not turn a done confirmation into a failure
setup legacy none 0
lb install-files A "$T/root" >/dev/null 2>&1 && mv "$T/boot/tryboot.txt" "$T/boot/config.txt"
lb install-files B "$T/root" >/dev/null 2>&1
mkdir -p "$T/boot/overlays/sub"; echo x > "$T/boot/overlays/sub/file"; chmod 555 "$T/boot/overlays/sub"
echo "root=/dev/mmcblk0p3 rauc.slot=B panic=10" > "$T/cmdline"; printf '\000\000\000\001' > "$T/flag"
if [ -w "$T/boot/overlays/sub" ] && echo y > "$T/boot/overlays/sub/probe" 2> /dev/null; then
  echo "[SKIP] undeletable legacy file (running as root: a read-only directory does not stop rm)"
else
  OUT="$(lb set-state B good 2>&1)"; RC=$?
  check "confirm succeeds although a legacy file cannot be removed" test "$RC" = 0
  check "the warning names the legacy root kernel files" out_says "warning: could not remove the legacy root kernel files"
  check "config.txt boots b/ all the same" grep -qx 'os_prefix=b/' "$T/boot/config.txt"
  check "tryboot.txt consumed all the same" test ! -e "$T/boot/tryboot.txt"
  check "B recorded good all the same" out_is good get-state B
  check "the trial counts as confirmed" refused trial-pending
fi
chmod 755 "$T/boot/overlays/sub"
UNIT="$HERE/../board/loopi/rootfs-overlay/etc/systemd/system/loopi-tryboot-guard.service"
check "guard service is ordered after boot.mount" grep -qx 'After=boot.mount' "$UNIT"
check "guard service does not depend on /boot (it is nofail)" bash -c "! grep -qE '^(Requires|BindsTo|Wants|RequiresMountsFor)=' '$UNIT'"
exit "$fail"
