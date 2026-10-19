#!/usr/bin/env bash
# Move a device running a pre-P4a image (kernel at the root of /boot, rootfs on p2, p3 empty, no RAUC) to the A/B
# layout over SSH, without taking the SD card out (P4a design §10). This is the first OTA, done by hand: the new rootfs
# goes to slot B (p3) and gets a trial boot; its agent confirms it after the first heartbeat, exactly as after a web
# update. If the trial fails, the device comes back on the old system, untouched, and this can be run again.
# Usage: os/scripts/migrate-to-ab.sh <device-ip> <os/out/loopi-dev-<version>.json> <backup dir>
#   The .squashfs next to the .json is written to p3. /data/keys and /data/config are first copied to
#   <backup dir>/data-keys-config.tar (mode 600, never printed, never committed): the way back after a re-flash.
set -euo pipefail
die() { echo "migrate-to-ab: $*" >&2; exit 1; }
[ $# = 3 ] || { echo "usage: $0 <device-ip> <bundle.json> <backup dir>" >&2; exit 2; }
host="$1"; desc="$2"; backup="$3"
ssh_() { ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$host" "$@"; }
version="$(jq -er .version "$desc")"
rootfs="${desc%.json}.squashfs"
[ -f "$rootfs" ] || die "$rootfs not found"
size="$(stat -c %s "$rootfs")"
sha="$(sha256sum "$rootfs" | cut -d' ' -f1)"

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

echo "== backup of /data/keys and /data/config to $backup"
mkdir -p "$backup"
chmod 700 "$backup"
( umask 077; ssh_ tar -C /data -cf - keys config > "$backup/data-keys-config.tar" )
# No -q: grep reads the whole listing, so tar is never killed by a closed pipe (pipefail would call that a failure).
tar -tf "$backup/data-keys-config.tar" | grep '^keys/' > /dev/null || die "the backup has no keys/"

echo "== writing the new rootfs ($version) to slot B"
ssh_ "dd of=/dev/mmcblk0p3 bs=1M conv=fsync 2>/dev/null" < "$rootfs"
# Drop the page cache first: the block device was just written through it, and the read-back must come from the card.
remote_sha="$(ssh_ "sync; echo 3 > /proc/sys/vm/drop_caches; head -c $size /dev/mmcblk0p3 | sha256sum" | cut -d' ' -f1)"
[ "$remote_sha" = "$sha" ] || die "slot B does not hold what was sent (sha256 differs)"

echo "== boot files for slot B and tryboot.txt, by the new system's own loopi-boot"
ssh_ 'set -u; m="$(mktemp -d)"; mount -t squashfs -o ro /dev/mmcblk0p3 "$m"; rc=0
      "$m/usr/libexec/loopi/loopi-boot" install-files B "$m" || rc=$?; umount "$m"; rmdir "$m"; exit $rc'
ssh_ 'grep -qx "os_prefix=b/" /boot/tryboot.txt' || die "tryboot.txt does not boot b/"

# Last step before the reboot, so that anything that failed above leaves the device exactly as it was.
echo "== a dev agent in /data/dev predates OTA and would never confirm the trial: set it aside"
ssh_ 'if [ -e /data/dev/loopi-agent ]; then mv /data/dev/loopi-agent /data/dev/loopi-agent.pre-ab; echo moved; fi'

echo "== pending update state for the new agent"
printf '{"command_id":null,"bundle":null,"version":"%s","target_slot":"B","phase":"rebooting","error":null}' "$version" \
  | ssh_ 'mkdir -p /data/ota && cat > /data/ota/pending.json.tmp && sync && mv /data/ota/pending.json.tmp /data/ota/pending.json && sync'

echo "== trial boot of slot B"
# The device keeps answering SSH for a while after the reboot command, so "it answers" proves nothing: wait for a boot id
# that differs from the one before.
old_boot="$(ssh_ cat /proc/sys/kernel/random/boot_id)"
ssh_ 'systemctl reboot --reboot-argument="0 tryboot"' || true
back=""
for _ in $(seq 1 60); do
  sleep 5
  now="$(ssh_ cat /proc/sys/kernel/random/boot_id 2>/dev/null)" || continue
  if [ "$now" != "$old_boot" ]; then back=1; break; fi
done
[ -n "$back" ] || die "the device did not come back within 5 minutes"
cmdline="$(ssh_ cat /proc/cmdline)" || die "the device stopped answering again"
case "$cmdline" in
  *rauc.slot=B*) echo "slot B is running; waiting for the agent to confirm it" ;;
  *) die "the trial did not start: the device is back on the old system (nothing changed); see its journal" ;;
esac
for _ in $(seq 1 60); do
  if ssh_ 'grep -qx "os_prefix=b/" /boot/config.txt'; then
    echo "== done: slot B ($version) is confirmed. The first web update will write slot A."
    exit 0
  fi
  sleep 5
done
die "slot B was not confirmed within 5 minutes; the guard reboots into the old system 10 minutes after boot"
