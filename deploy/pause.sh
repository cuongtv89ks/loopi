#!/usr/bin/env bash
# Pause Loopi on the brain PC: ask every online device to pause recording (it keeps that across reboots), wait until
# its heartbeat says so, then stop the brain's containers. The containers and every volume stay as they are: nothing
# is deleted, and with "restart: unless-stopped" they stay stopped after a reboot. deploy/resume.sh starts them again
# and resumes the devices this script paused.
# Usage: deploy/pause.sh            (from any directory; the compose project "deploy" of this directory)
set -euo pipefail
cd "$(dirname "$0")"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/loopi/paused-devices"
WAIT_S=90

compose() { docker compose -p deploy "$@"; }
sql() { compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "$1"' sh "$1"; }
post() {  # an htmx POST to the brain's web routes, from inside the api container
  compose exec -T api python -c 'import sys, urllib.request
r = urllib.request.urlopen(urllib.request.Request("http://localhost:8000" + sys.argv[1], data=b"", headers={"HX-Request": "true"}))
print(r.status)' "$1" >/dev/null
}

running="$(compose ps --status running --services 2>/dev/null || true)"
if [ -z "$running" ]; then
  echo "Loopi is already stopped (no running container in the compose project 'deploy')."
  exit 0
fi

mkdir -p "$(dirname "$STATE")"
touch "$STATE"
if grep -qx api <<<"$running" && grep -qx db <<<"$running"; then
  # The paused flag is read from a heartbeat sent after this script started (heartbeats come every 15 s): an older
  # one can predate a pause or resume that is still on its way, as right after resume.sh.
  started="$(date +%s)"
  online="$(sql "SELECT id FROM device WHERE NOT revoked AND last_heartbeat_at > now() - interval '2 minutes' ORDER BY id")"
  fresh_deadline=$((SECONDS + 40))
  for id in $online; do
    [[ "$id" =~ ^[0-9A-Z]{26}$ ]] || continue
    until [ "$(sql "SELECT count(*) FROM device WHERE id = '$id' AND last_heartbeat_at > to_timestamp($started)")" = 1 ]; do
      [ "$SECONDS" -lt "$fresh_deadline" ] || { echo "warning: no fresh heartbeat from device $id" >&2; break; }
      sleep 3
    done
  done
  # Devices heard from in the last 2 minutes that are recording (not paused, not revoked).
  ids="$(sql "SELECT id FROM device WHERE NOT revoked AND last_heartbeat_at > now() - interval '2 minutes'
              AND coalesce(last_heartbeat->>'paused', 'false') <> 'true' ORDER BY id")"
  sent="$(date +%s)"
  for id in $ids; do
    [[ "$id" =~ ^[0-9A-Z]{26}$ ]] || { echo "skipping an unexpected device id" >&2; continue; }
    post "/devices/$id/commands/pause"
    grep -qx "$id" "$STATE" || echo "$id" >>"$STATE"
    echo "pause sent to device $id"
  done
  deadline=$((SECONDS + WAIT_S))
  for id in $ids; do
    until [ "$(sql "SELECT coalesce(last_heartbeat->>'paused', '') FROM device WHERE id = '$id'
                    AND last_heartbeat_at > to_timestamp($sent)")" = true ]; do
      if [ "$SECONDS" -ge "$deadline" ]; then
        echo "warning: device $id did not confirm the pause within ${WAIT_S}s; it keeps recording into its own" \
             "queue until resume.sh (or the web) reaches it" >&2
        break
      fi
      sleep 5
    done
    [ "$SECONDS" -ge "$deadline" ] || echo "device $id is paused"
  done
else
  echo "warning: the api or db container is not running, so no device was asked to pause" >&2
fi

compose stop
echo "Loopi is paused: every container of the compose project 'deploy' is stopped; data and volumes are kept."
echo "Start it again with: $(pwd)/resume.sh"
