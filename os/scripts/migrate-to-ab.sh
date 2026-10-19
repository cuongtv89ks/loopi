#!/usr/bin/env bash
# Move a device running a pre-P4a image (kernel at the root of /boot, rootfs on p2, p3 empty, no RAUC) to the A/B
# layout over SSH, without taking the SD card out (P4a design §10). This is the first OTA, done by hand: the new rootfs
# goes to slot B (p3) and gets a trial boot; its agent confirms it after the first heartbeat, exactly as after a web
# update. If the trial fails, the device comes back on the old system and this can be run again.
# Usage: os/scripts/migrate-to-ab.sh <device-ip> <os/out/loopi-dev-<version>.json> <backup dir>
#   Dev images only: the new system is watched over SSH, which a prod image does not have.
#   The .squashfs next to the .json is written to p3. /data/keys and /data/config are first copied to
#   <backup dir>/data-keys-config.tar (mode 600, never printed, never committed; the directory must be outside any git
#   work tree): the way back after a re-flash. A good backup is only ever replaced by another verified one.
#   What it changes on the old system besides p3 and /boot/b, /boot/tryboot.txt (only read on a "0 tryboot" reboot):
#   /data/ota/pending.json (written for the new agent) and the dev agent /data/dev/loopi-agent, which is set aside as
#   loopi-agent.pre-ab[-<time>] because it predates OTA and would never confirm the trial. Both are put back whenever the
#   device stays on (or returns to) the old system, and the script says so; when it cannot reach the device it prints
#   the commands to do it by hand.
set -euo pipefail
# How long the device gets to answer again after the reboot command, and how long the new agent gets to confirm slot B once
# the device is back (the guard reboots an unconfirmed trial 10 minutes after boot). Environment overrides are test hooks.
BACK_WAIT="${LOOPI_MIGRATE_BACK_WAIT:-300}"
CONFIRM_WAIT="${LOOPI_MIGRATE_CONFIRM_WAIT:-900}"
POLL="${LOOPI_MIGRATE_POLL:-5}"
die() { echo "migrate-to-ab: $*" >&2; exit 1; }
human() { if [ "$1" -ge 60 ]; then echo "$(($1 / 60)) minutes"; else echo "$1 seconds"; fi; }
[ $# = 3 ] || { echo "usage: $0 <device-ip> <bundle.json> <backup dir>" >&2; exit 2; }
host="$1"; desc="$2"; backup="$3"
ssh_() { ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 "root@$host" "$@"; }

# Everything that can be refused is refused before the device is touched.
[ -f "$desc" ] || die "$desc not found"
variant="$(jq -er .variant "$desc")" || die "$desc has no variant"
[ "$variant" = dev ] || die "$desc is a $variant image; only a dev image can be moved (the trial is watched over SSH, which prod does not have)"
version="$(jq -er .version "$desc")" || die "$desc has no version"
[[ "$version" =~ ^[0-9A-Za-z.+-]{1,64}$ ]] || die "the version in $desc is not a valid version string"
rootfs="${desc%.json}.squashfs"
[ -f "$rootfs" ] || die "$rootfs not found"
size="$(stat -c %s "$rootfs")"
sha="$(sha256sum "$rootfs" | cut -d' ' -f1)"
probe="$backup"
while [ ! -d "$probe" ]; do probe="$(dirname "$probe")"; done
[ "$(git -C "$probe" rev-parse --is-inside-work-tree 2>/dev/null || true)" != true ] \
  || die "$backup is inside a git work tree; the backup holds the device's keys and must never be committed: use a directory outside any repository"

# State of this run, for the exit handler.
tmp_tar=""           # the backup being written
rollback_needed=0    # 1 from the first change to the old system's own state until the reboot is under way
has_agent=0          # a dev agent was in /data/dev when the run started
aside=/data/dev/loopi-agent.pre-ab

print_manual_restore() {
  local cmd="rm -f /data/ota/pending.json"
  if [ "$has_agent" = 1 ]; then cmd="$cmd; mv $aside /data/dev/loopi-agent"; fi
  echo "migrate-to-ab: when the old system is up again, put its state back with: ssh root@$host '$cmd'" >&2
}

# The device is on the old system: remove the pending update state and move the dev agent back, saying what was done.
rollback() {
  local failed=0 out
  echo "migrate-to-ab: the device is on the old system; putting back what this run changed there:" >&2
  if out="$(ssh_ 'if [ -e /data/ota/pending.json ] || [ -e /data/ota/pending.json.tmp ]; then
                    rm -f /data/ota/pending.json /data/ota/pending.json.tmp && echo removed
                  else echo absent; fi; sync')"; then
    case "$out" in
      removed) echo "  /data/ota/pending.json removed" >&2 ;;
      *) echo "  /data/ota/pending.json was never written" >&2 ;;
    esac
  else
    failed=1
  fi
  if [ "$has_agent" = 1 ]; then
    # Refuses to overwrite a loopi-agent that appeared meanwhile.
    if out="$(ssh_ "if [ ! -e '$aside' ]; then echo unmoved
                    elif [ -e /data/dev/loopi-agent ]; then exit 1
                    else mv '$aside' /data/dev/loopi-agent && sync && echo restored; fi")"; then
      case "$out" in
        restored) echo "  /data/dev/loopi-agent moved back from $aside" >&2 ;;
        *) echo "  /data/dev/loopi-agent was never moved: it is where it was" >&2 ;;
      esac
    else
      failed=1
    fi
  fi
  if [ "$failed" = 1 ]; then
    echo "migrate-to-ab: could not put everything back." >&2
    print_manual_restore
  else
    echo "  /boot/b and /boot/tryboot.txt stay: only a '0 tryboot' reboot reads them and the next run replaces them" >&2
  fi
}

abort_on_old_system() {
  echo "migrate-to-ab: $*" >&2
  rollback_needed=0
  rollback
  exit 1
}

# The device did not answer: it may be running anything. Nothing is touched, the owner is told what to do.
die_unreachable() {
  echo "migrate-to-ab: $*" >&2
  rollback_needed=0
  echo "migrate-to-ab: power-cycle the device (unplug the power, plug it back in): a new system that hangs before the watchdog" >&2
  echo "migrate-to-ab: starts never reboots by itself, and a power cycle boots the old system (config.txt was not touched)." >&2
  print_manual_restore
  exit 1
}

on_exit() {
  local rc=$?
  [ -z "$tmp_tar" ] || rm -f "$tmp_tar"
  if [ "$rc" != 0 ] && [ "$rollback_needed" = 1 ]; then
    rollback_needed=0
    rollback
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT TERM HUP

echo "== checks"
cmdline="$(ssh_ cat /proc/cmdline)"
case "$cmdline" in *rauc.slot=*) die "the device already boots an A/B slot; use the web update instead" ;; esac
case " $cmdline " in *" root=/dev/mmcblk0p2 "*) ;; *) die "unexpected root= on the device: $cmdline" ;; esac
ssh_ '[ -b /dev/mmcblk0p3 ]' || die "no /dev/mmcblk0p3 on the device"
ssh_ '! grep -q "^/dev/mmcblk0p3 " /proc/mounts' || die "/dev/mmcblk0p3 is mounted"
p3="$(ssh_ blockdev --getsize64 /dev/mmcblk0p3)"
[ "$p3" -ge "$size" ] || die "p3 ($p3 bytes) is smaller than the rootfs ($size bytes)"
free_kb="$(ssh_ df -k /boot | awk 'NR == 2 { print $4 }')"
[ "$free_kb" -ge 12000 ] || die "only ${free_kb} KiB free on /boot; slot B needs about 9 MiB"
found="$(ssh_ '[ ! -e /data/dev/loopi-agent ] || echo agent=1; [ ! -e /data/dev/loopi-agent.pre-ab ] || echo taken=1; true')"
case "$found" in *agent=1*) has_agent=1 ;; esac
# Never overwrite an agent set aside by an earlier run.
case "$found" in *taken=1*) aside="$aside-$(date +%s)" ;; esac

echo "== backup of /data/keys and /data/config to $backup"
mkdir -p "$backup"
chmod 700 "$backup"
# Written beside the final name (mode 600) and renamed only when it lists both directories: a failed run never empties a
# backup that is already good.
tmp_tar="$(mktemp "$backup/.data-keys-config.XXXXXX")"
if ! ( umask 077; ssh_ tar -C /data -cf - keys config > "$tmp_tar" ); then
  die "could not read /data/keys and /data/config from the device; an earlier backup in $backup is untouched"
fi
listing="$(tar -tf "$tmp_tar")" || die "the backup is not a readable tar; an earlier backup in $backup is untouched"
grep -q '^keys/' <<<"$listing" || die "the backup has no keys/; an earlier backup in $backup is untouched"
grep -q '^config/' <<<"$listing" || die "the backup has no config/; an earlier backup in $backup is untouched"
chmod 600 "$tmp_tar"
mv -f "$tmp_tar" "$backup/data-keys-config.tar"
tmp_tar=""

echo "== writing the new rootfs ($version) to slot B"
ssh_ "dd of=/dev/mmcblk0p3 bs=1M conv=fsync 2>/dev/null" < "$rootfs"
# Drop the page cache first: the block device was just written through it, and the read-back must come from the card.
remote_sha="$(ssh_ "sync; echo 3 > /proc/sys/vm/drop_caches; head -c $size /dev/mmcblk0p3 | sha256sum" | cut -d' ' -f1)"
[ "$remote_sha" = "$sha" ] || die "slot B does not hold what was sent (sha256 differs)"

echo "== boot files for slot B and tryboot.txt, by the new system's own loopi-boot"
ssh_ 'set -u; m="$(mktemp -d)"; mount -t squashfs -o ro /dev/mmcblk0p3 "$m"; rc=0
      "$m/usr/libexec/loopi/loopi-boot" install-files B "$m" || rc=$?; umount "$m"; rmdir "$m"; exit $rc'
ssh_ 'grep -qx "os_prefix=b/" /boot/tryboot.txt' || die "tryboot.txt does not boot b/"

# From here on the old system's own state is changed; the exit handler puts it back on any failure up to the reboot.
boot_info() { ssh_ 'cat /proc/sys/kernel/random/boot_id /proc/cmdline'; }
first="$(boot_info)" || die "could not read the boot id of the device"
old_boot="${first%%$'\n'*}"
[ -n "$old_boot" ] || die "could not read the boot id of the device"
rollback_needed=1

echo "== pending update state for the new agent"
printf '{"command_id":null,"bundle":null,"version":"%s","target_slot":"B","phase":"rebooting","error":null}' "$version" \
  | ssh_ 'mkdir -p /data/ota && cat > /data/ota/pending.json.tmp && sync && mv /data/ota/pending.json.tmp /data/ota/pending.json && sync' \
  || die "could not write /data/ota/pending.json"

# The very last step before the reboot (the old agent ignores pending.json, and nothing below can fail before the reboot but
# the reboot itself).
if [ "$has_agent" = 1 ]; then
  echo "== setting the dev agent aside as $aside (it predates OTA and would never confirm the trial)"
  ssh_ "[ ! -e '$aside' ] && mv /data/dev/loopi-agent '$aside' && sync" || die "could not set /data/dev/loopi-agent aside"
fi

echo "== trial boot of slot B"
reboot_rc=0
ssh_ 'systemctl reboot --reboot-argument="0 tryboot"' || reboot_rc=$?
# 0 or 255 (the connection went away with the device): the reboot is under way. Anything else: it was refused.
if [ "$reboot_rc" != 0 ] && [ "$reboot_rc" != 255 ]; then
  abort_on_old_system "the reboot was refused (exit status $reboot_rc), so the device did not restart"
fi
rollback_needed=0
# The device keeps answering SSH for a while after the reboot command, so "it answers" proves nothing: wait for a boot id
# that differs from the one before.
deadline=$((SECONDS + BACK_WAIT))
back=""
answered_old=0
while [ "$SECONDS" -lt "$deadline" ]; do
  sleep "$POLL"
  if info="$(boot_info 2>/dev/null)" && [ -n "$info" ]; then
    if [ "${info%%$'\n'*}" != "$old_boot" ]; then back=1; break; fi
    answered_old=1
  else
    answered_old=0
  fi
done
if [ -z "$back" ]; then
  if [ "$answered_old" = 1 ]; then
    abort_on_old_system "the device did not restart: it still answers on the same boot after $(human "$BACK_WAIT")"
  fi
  die_unreachable "the device did not come back within $(human "$BACK_WAIT")"
fi
case "${info#*$'\n'}" in
  *rauc.slot=B*) echo "slot B is running; waiting up to $(human "$CONFIRM_WAIT") for the agent to confirm it" ;;
  *) abort_on_old_system "the trial did not start: the device is back on the old system; see its journal" ;;
esac
deadline=$((SECONDS + CONFIRM_WAIT))
while [ "$SECONDS" -lt "$deadline" ]; do
  if status="$(ssh_ 'if grep -qx "os_prefix=b/" /boot/config.txt; then echo confirmed; else cat /proc/cmdline; fi' 2>/dev/null)"; then
    case "$status" in
      confirmed)
        echo "== done: slot B ($version) is confirmed. The first web update will write slot A."
        if [ "$has_agent" = 1 ]; then echo "The old dev agent is at $aside on the device."; fi
        exit 0 ;;
      *rauc.slot=B*) ;;
      *) abort_on_old_system "slot B was not confirmed in time and the guard put the device back on the old system; see the journal of slot B's agent (journalctl -b -1 -u loopi-agent)" ;;
    esac
  fi
  sleep "$POLL"
done
echo "migrate-to-ab: slot B was not confirmed within $(human "$CONFIRM_WAIT") of coming back, but the device did not return to the old system." >&2
echo "migrate-to-ab: the guard should have rebooted it 10 minutes after boot. Look at it over SSH (journalctl -u loopi-agent), or power-cycle it to boot the old system." >&2
print_manual_restore
exit 1
