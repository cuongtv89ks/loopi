#!/usr/bin/env bash
# Host test of os/scripts/migrate-to-ab.sh against a fake `ssh` (a state machine standing in for the pre-P4a device; it
# connects nowhere). Remote commands that only touch /data run for real in a sandbox, under BusyBox sh like the device
# (REMOTE_SH overrides). Usage: os/tests/test-migrate-to-ab.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MIGRATE="${MIGRATE:-$HERE/../scripts/migrate-to-ab.sh}"
if command -v busybox > /dev/null; then export REMOTE_SH="${REMOTE_SH:-busybox sh}"; else export REMOTE_SH="${REMOTE_SH:-dash}"; fi
fail=0
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "[ OK ] $name"; else echo "[FAIL] $name"; fail=1; fi; }
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
mkdir -p "$W/bin"

cat > "$W/bin/sync" <<'EOF'
#!/bin/sh
exit 0
EOF
cat > "$W/bin/ssh" <<'EOF'
#!/bin/bash
# Fake ssh. MOCK_ST = state dir, SCENARIO = what goes wrong (or ok).
ST="${MOCK_ST:?}"; SC="${SCENARIO:-ok}"
while [ "${1:-}" = -o ]; do shift 2; done
shift  # root@host
cmd="$*"
printf '%s\n' "$cmd" >> "$ST/calls"
phase="$(cat "$ST/phase" 2>/dev/null || echo old)"   # old | going-down | down | newboot | oldagain
count() { local n; n="$(cat "$ST/$1" 2>/dev/null || echo 0)"; n=$((n + 1)); echo "$n" > "$ST/$1"; echo "$n"; }
cmdline_old="root=/dev/mmcblk0p2 rootfstype=squashfs ro rootwait console=serial0,115200"
cmdline_new="root=/dev/mmcblk0p3 rootfstype=squashfs ro rootwait quiet rauc.slot=B panic=10"
case "$cmd" in
  "cat /proc/cmdline")
    if [ "$SC" = ab ]; then echo "root=/dev/mmcblk0p2 ro rauc.slot=A"; else echo "$cmdline_old"; fi ;;
  "[ -b /dev/mmcblk0p3 ]"|'! grep -q "^/dev/mmcblk0p3 " /proc/mounts') exit 0 ;;
  "blockdev --getsize64 /dev/mmcblk0p3") echo 1000000000 ;;
  "df -k /boot") printf 'Filesystem 1K-blocks Used Available Use%% Mounted on\n/dev/mmcblk0p1 65536 10000 55536 15%% /boot\n' ;;
  "tar -C /data -cf - keys config")
    case "$SC" in
      tarfail) printf 'partial'; exit 1 ;;
      tarnoconfig) d="$(mktemp -d)"; mkdir "$d/keys"; echo k > "$d/keys/a"; tar -C "$d" -cf - keys; rm -rf "$d" ;;
      *) d="$(mktemp -d)"; mkdir "$d/keys" "$d/config"; echo k > "$d/keys/a"; echo c > "$d/config/b"
         tar -C "$d" -cf - keys config; rm -rf "$d" ;;
    esac ;;
  "dd of=/dev/mmcblk0p3 bs=1M conv=fsync 2>/dev/null") cat > "$ST/p3" ;;
  "sync; echo 3 > /proc/sys/vm/drop_caches; head -c "*" /dev/mmcblk0p3 | sha256sum")
    n="${cmd#*head -c }"; n="${n%% *}"; head -c "$n" "$ST/p3" | sha256sum ;;
  "set -u; m="*) touch "$ST/tryboot" ;;
  'grep -qx "os_prefix=b/" /boot/tryboot.txt') [ -e "$ST/tryboot" ] ;;
  "mkdir -p /data/ota && cat"*)
    if [ "$SC" = pendingfail ]; then cat > /dev/null; exit 1; fi
    eval_cmd="${cmd//\/data\//$ST/data/}"; exec $REMOTE_SH -c "$eval_cmd" ;;
  *"&& mv /data/dev/loopi-agent '"*)
    if [ "$SC" = agentmovefail ]; then exit 1; fi
    eval_cmd="${cmd//\/data\//$ST/data/}"; exec $REMOTE_SH -c "$eval_cmd" ;;
  *"/data/dev/"*|*"/data/ota/"*)
    eval_cmd="${cmd//\/data\//$ST/data/}"; exec $REMOTE_SH -c "$eval_cmd" ;;
  "cat /proc/sys/kernel/random/boot_id /proc/cmdline")
    case "$phase" in
      old) [ "$SC" != bootidfail ] || exit 255; echo boot-1; echo "$cmdline_old" ;;
      going-down) k="$(count bi)"
        if [ "$k" -le 2 ]; then touch "$ST/answered-after-reboot-command"; echo boot-1; echo "$cmdline_old"
        else echo down > "$ST/phase"; echo 0 > "$ST/bi"; exit 255; fi ;;
      down) k="$(count bi)"; [ "$SC" != never ] || exit 255
        [ "$k" -gt 3 ] || exit 255
        if [ "$SC" = notrial ]; then echo oldagain > "$ST/phase"; else echo newboot > "$ST/phase"; fi
        echo boot-2; if [ "$SC" = notrial ]; then echo "$cmdline_old"; else echo "$cmdline_new"; fi ;;
      newboot) echo boot-2; echo "$cmdline_new" ;;
      oldagain) echo boot-2; echo "$cmdline_old" ;;
    esac ;;
  'systemctl reboot'*)
    case "$SC" in
      refused) echo "Failed to reboot: Access denied" >&2; exit 1 ;;
      noreboot) exit 0 ;;
      *) echo going-down > "$ST/phase"; echo 0 > "$ST/bi"; exit 255 ;;
    esac ;;
  'if grep -qx "os_prefix=b/" /boot/config.txt'*)
    k="$(count conf)"
    case "$SC" in
      unconfirmed) echo "$cmdline_new" ;;
      guard) if [ "$k" -le 2 ]; then echo "$cmdline_new"; else echo oldagain > "$ST/phase"; echo "$cmdline_old"; fi ;;
      *) if [ "$k" -ge 3 ]; then echo confirmed; else echo "$cmdline_new"; fi ;;
    esac ;;
  *) echo "UNEXPECTED ssh command: $cmd" >&2; exit 99 ;;
esac
EOF
chmod 755 "$W/bin/ssh" "$W/bin/sync"

# run <scenario>: a fresh sandbox device and build output; leaves $OUT, $RC, $ST (device state), $BK (backup dir).
# NOAGENT=1: no dev agent on the device; TAKEN=1: a loopi-agent.pre-ab from an earlier run exists; VARIANT, VERSION: the description.
run() {
  local sc="$1" d="$W/run-$1-$RANDOM"
  ST="$d/st"; BK="$d/backup"; mkdir -p "$ST/data/dev" "$ST/data/ota" "$d/out"
  [ "${NOAGENT:-0}" = 1 ] || echo dev-agent-binary > "$ST/data/dev/loopi-agent"
  [ "${TAKEN:-0}" != 1 ] || echo older-agent > "$ST/data/dev/loopi-agent.pre-ab"
  head -c 5000 /dev/urandom > "$d/out/loopi-dev-9.9.squashfs"
  jq -n --arg v "${VERSION:-9.9}" --arg var "${VARIANT:-dev}" '{version: $v, variant: $var}' > "$d/out/loopi-dev-9.9.json"
  [ -z "${BACKUP:-}" ] || BK="$BACKUP"
  OUT="$(MOCK_ST="$ST" SCENARIO="$sc" PATH="$W/bin:$PATH" LOOPI_MIGRATE_POLL=0.05 LOOPI_MIGRATE_BACK_WAIT=4 \
    LOOPI_MIGRATE_CONFIRM_WAIT=4 "$MIGRATE" 192.0.2.1 "$d/out/loopi-dev-9.9.json" "$BK" 2>&1)"; RC=$?
}
says() { case "$OUT" in *"$1"*) return 0 ;; esac; return 1; }
not() { ! "$@"; }
called() { grep -qF -- "$1" "$ST/calls" 2>/dev/null; }
line_of() { grep -nF -- "$1" "$ST/calls" | head -n 1 | cut -d: -f1; }
before() { [ -n "$(line_of "$1")" ] && [ -n "$(line_of "$2")" ] && [ "$(line_of "$1")" -lt "$(line_of "$2")" ]; }
agent_in_place() { [ "$(cat "$ST/data/dev/loopi-agent" 2>/dev/null)" = dev-agent-binary ]; }
pending_absent() { [ ! -e "$ST/data/ota/pending.json" ] && [ ! -e "$ST/data/ota/pending.json.tmp" ]; }
no_aside() { ! ls "$ST/data/dev" | grep -q 'pre-ab'; }
shown() { [ "$fail" = 0 ] || [ -z "${SHOW:-}" ] || { echo "---- output of $1:"; echo "$OUT"; }; }
PENDING='{"command_id":null,"bundle":null,"version":"9.9","target_slot":"B","phase":"rebooting","error":null}'

run ok
check "ok: succeeds" test "$RC" = 0
check "ok: says it is confirmed" says "slot B (9.9) is confirmed"
check "ok: pending.json is exactly what the agent reads" test "$(cat "$ST/data/ota/pending.json")" = "$PENDING"
check "ok: the dev agent is set aside" test "$(cat "$ST/data/dev/loopi-agent.pre-ab")" = dev-agent-binary
check "ok: the old system kept answering after the reboot command and was not mistaken for the new boot" test -e "$ST/answered-after-reboot-command"
check "ok: order dd, boot files, pending.json, agent move, reboot" bash -c "
  $(declare -f line_of before); ST='$ST'; before 'dd of=' 'install-files' && before 'install-files' 'mkdir -p /data/ota' &&
  before 'mkdir -p /data/ota' 'mv /data/dev/loopi-agent' && before 'mv /data/dev/loopi-agent' 'systemctl reboot'"
check "ok: backup is mode 600 in a 700 directory" test "$(stat -c '%a %U' "$BK/data-keys-config.tar")" = "600 $(id -un)" -a "$(stat -c %a "$BK")" = 700
check "ok: backup lists keys/ and config/" bash -c "tar -tf '$BK/data-keys-config.tar' | grep -q '^keys/' && tar -tf '$BK/data-keys-config.tar' | grep -q '^config/'"
check "ok: no temporary backup left" bash -c "! ls -A '$BK' | grep -q '^\\.'"

# A failed run must never empty a backup that is already good: only a verified new tar replaces it.
BACKUP="$W/keepdir"; mkdir -p "$BACKUP"; printf 'GOOD-BACKUP' > "$BACKUP/data-keys-config.tar"
for sc in tarfail tarnoconfig; do
  run "$sc"
  check "$sc: fails" test "$RC" != 0
  check "$sc: the earlier backup is untouched" test "$(cat "$BACKUP/data-keys-config.tar")" = GOOD-BACKUP
  check "$sc: no temporary backup left" bash -c "! ls -A '$BACKUP' | grep -q '^\\.'"
  check "$sc: nothing was written to the device" not called "dd of="
  check "$sc: says the earlier backup is untouched" says "earlier backup"
done
run ok
check "a good run replaces the earlier backup" bash -c "tar -tf '$BACKUP/data-keys-config.tar' | grep -q '^keys/'"
unset BACKUP

for sc in refused noreboot agentmovefail pendingfail; do
  run "$sc"
  check "$sc: fails" test "$RC" != 0
  check "$sc: the dev agent is back where it was" agent_in_place
  check "$sc: no pending.json" pending_absent
  check "$sc: nothing left aside" no_aside
done
run refused;       check "refused: names the refusal" says "the reboot was refused"
check "refused: says pending.json was removed and the agent moved back" says "pending.json removed"
check "refused: says the agent moved back" says "moved back from /data/dev/loopi-agent.pre-ab"
run noreboot;      check "noreboot: says the device did not restart" says "did not restart"
check "noreboot: says the agent moved back" says "moved back from /data/dev/loopi-agent.pre-ab"
run agentmovefail; check "agentmovefail: says it could not set the agent aside" says "could not set /data/dev/loopi-agent aside"
check "agentmovefail: says pending.json was removed" says "pending.json removed"
check "agentmovefail: says the agent never moved" says "never moved"
run pendingfail;   check "pendingfail: says it could not write pending.json" says "could not write /data/ota/pending.json"
check "pendingfail: says nothing was written" says "pending.json was never written"
check "pendingfail: says the agent never moved" says "never moved"
check "pendingfail: the agent was never touched" not called "mv /data/dev/loopi-agent"

run bootidfail
check "bootidfail: fails" test "$RC" != 0
check "bootidfail: nothing changed on the old system" bash -c "[ ! -e '$ST/data/ota/pending.json' ] && ! ls '$ST/data/dev' | grep -q pre-ab"
check "bootidfail: pending.json never written" not called "mkdir -p /data/ota"

for sc in notrial guard; do
  run "$sc"
  check "$sc: fails" test "$RC" != 0
  check "$sc: the device is on the old system, the agent is moved back" agent_in_place
  check "$sc: pending.json removed" pending_absent
  check "$sc: nothing left aside" no_aside
  check "$sc: says what it restored" says "moved back from /data/dev/loopi-agent.pre-ab"
done
run notrial; check "notrial: says the trial did not start" says "the trial did not start"
run guard;   check "guard: says the guard put the device back" says "the guard put the device back"

run unconfirmed
check "unconfirmed: fails" test "$RC" != 0
check "unconfirmed: says the real wait" says "within 4 seconds"
check "unconfirmed: the device runs slot B, so nothing is moved back" test -e "$ST/data/ota/pending.json" -a -e "$ST/data/dev/loopi-agent.pre-ab"
check "unconfirmed: tells how to restore by hand" says "mv /data/dev/loopi-agent.pre-ab /data/dev/loopi-agent"
check "unconfirmed: the command to restore by hand guards itself on the kernel command line" says "grep -q rauc.slot= /proc/cmdline ||"
check "unconfirmed: the command only moves the agent back when none is there" says "[ -e /data/dev/loopi-agent ] || mv /data/dev/loopi-agent.pre-ab /data/dev/loopi-agent"
run never
check "never: fails" test "$RC" != 0
check "never: tells the owner to power-cycle" says "power-cycle"
check "never: says the real wait" says "within 4 seconds"
check "never: tells how to restore by hand" says "rm -f /data/ota/pending.json; [ -e /data/dev/loopi-agent ] || mv /data/dev/loopi-agent.pre-ab /data/dev/loopi-agent"
check "never: the command to restore by hand guards itself on the kernel command line" says "grep -q rauc.slot= /proc/cmdline ||"
check "never: does not claim the power cycle surely boots the old system" not says "boots the old system (config.txt was not touched)"
check "never: says it cannot tell whether slot B was confirmed meanwhile" says "cannot tell whether slot B was confirmed"
check "never: does not touch an unreachable device's state" test -e "$ST/data/ota/pending.json"

# The printed command, run on the sandbox as the owner would on the device (/data and /proc/cmdline redirected).
manual_cmd() { printf '%s\n' "$OUT" | sed -n "s/.*ssh root@[^ ]* '\(.*\)'\$/\1/p" | tail -n 1; }
run_manual() {
  local c; c="$(manual_cmd)"; [ -n "$c" ] || return 1
  c="${c//\/data\//$ST/data/}"; c="${c//\/proc\/cmdline/$ST/cmdline}"
  $REMOTE_SH -c "$c"
}
echo "root=/dev/mmcblk0p3 ro rauc.slot=B" > "$ST/cmdline"
check "restore by hand on a device that boots a confirmed slot B: runs" run_manual
check "restore by hand on slot B: pending.json is left alone" test -e "$ST/data/ota/pending.json"
check "restore by hand on slot B: the agent stays set aside" test -e "$ST/data/dev/loopi-agent.pre-ab" -a ! -e "$ST/data/dev/loopi-agent"
echo "root=/dev/mmcblk0p2 ro rootwait" > "$ST/cmdline"
check "restore by hand on the old system: runs" run_manual
check "restore by hand on the old system: pending.json removed" pending_absent
check "restore by hand on the old system: the agent is back" agent_in_place
check "restore by hand on the old system: nothing left aside" no_aside
check "restore by hand again: harmless" run_manual
check "restore by hand again: the agent is still there" agent_in_place
echo a-newer-agent > "$ST/data/dev/loopi-agent"; echo dev-agent-binary > "$ST/data/dev/loopi-agent.pre-ab"
run_manual
check "restore by hand: an agent that appeared meanwhile is never overwritten" test "$(cat "$ST/data/dev/loopi-agent")" = a-newer-agent -a -e "$ST/data/dev/loopi-agent.pre-ab"
NOAGENT=1 run never
check "never, no dev agent: the command only removes pending.json, guarded" says "grep -q rauc.slot= /proc/cmdline || rm -f /data/ota/pending.json'"
check "never, no dev agent: no move in the command" not says "mv /data/dev"
echo "root=/dev/mmcblk0p2 ro rootwait" > "$ST/cmdline"
check "never, no dev agent: the command runs on the old system" run_manual
check "never, no dev agent: pending.json removed, nothing invented" bash -c "[ ! -e '$ST/data/dev/loopi-agent' ] && [ ! -e '$ST/data/ota/pending.json' ]"

TAKEN=1 run ok
check "taken: succeeds" test "$RC" = 0
check "taken: the agent set aside earlier is untouched" test "$(cat "$ST/data/dev/loopi-agent.pre-ab")" = older-agent
check "taken: this run's agent has its own name" bash -c "grep -l dev-agent-binary '$ST'/data/dev/loopi-agent.pre-ab-* >/dev/null"
TAKEN=1 run refused
check "taken+refused: the agent comes back" agent_in_place
check "taken+refused: the older set-aside agent is untouched" test "$(cat "$ST/data/dev/loopi-agent.pre-ab")" = older-agent
check "taken+refused: nothing else left aside" bash -c "! ls '$ST/data/dev' | grep -q 'pre-ab-'"

NOAGENT=1 run ok
check "no dev agent: succeeds" test "$RC" = 0
check "no dev agent: nothing is set aside" no_aside
NOAGENT=1 run refused
check "no dev agent, refused: pending.json removed, nothing invented" bash -c "[ ! -e '$ST/data/dev/loopi-agent' ] && [ ! -e '$ST/data/ota/pending.json' ]"

run ab
check "an A/B device is refused" test "$RC" != 0
check "an A/B device: nothing written" not called "dd of="

VARIANT=prod run ok
check "a prod description is refused" test "$RC" != 0
check "a prod description: says why" says "prod image"
check "a prod description: refused before any ssh" test ! -e "$ST/calls"
check "a prod description: no backup directory made" test ! -e "$BK"
VERSION='1.0"; rm -rf /' run ok
check "a version that is not a version string is refused" test "$RC" != 0
check "a bad version: refused before any ssh" test ! -e "$ST/calls"

G="$W/repo"; mkdir -p "$G/sub"; git -C "$G" init -q
BACKUP="$G/sub/keys" run ok
check "a backup directory inside a git work tree is refused" test "$RC" != 0
check "a backup in a work tree: says so" says "git work tree"
check "a backup in a work tree: refused before any ssh" test ! -e "$ST/calls"
check "a backup in a work tree: nothing created" test ! -e "$G/sub/keys"
BACKUP="$W/outside/new/dir" run ok
check "a new backup directory outside any work tree is fine" test "$RC" = 0

exit "$fail"
