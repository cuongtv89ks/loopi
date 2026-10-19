#!/usr/bin/env bash
# Resume Loopi on the brain PC after deploy/pause.sh: start the brain's containers again (the same containers, no
# rebuild), wait until they are healthy, then resume the devices pause.sh paused. A device still away (powered off,
# no WiFi) gets the resume command at its next heartbeat.
# Usage: deploy/resume.sh           (from any directory; the compose project "deploy" of this directory)
set -euo pipefail
cd "$(dirname "$0")"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/loopi/paused-devices"
WAIT_S=300

compose() { docker compose -p deploy "$@"; }
post() {  # an htmx POST to the brain's web routes, from inside the api container
  compose exec -T api python -c 'import sys, urllib.request
r = urllib.request.urlopen(urllib.request.Request("http://localhost:8000" + sys.argv[1], data=b"", headers={"HX-Request": "true"}))
print(r.status)' "$1" >/dev/null
}

if [ -z "$(compose ps -a --services 2>/dev/null)" ]; then
  echo "No container of the compose project 'deploy' exists here: deploy it first (see README.md, 'Deploy')." >&2
  exit 1
fi

compose start
deadline=$((SECONDS + WAIT_S))
for svc in db api worker models; do
  until [ "$(compose ps "$svc" --format '{{.Health}}' 2>/dev/null)" = healthy ]; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "error: $svc is not healthy after ${WAIT_S}s; see: docker compose -p deploy logs $svc" >&2
      exit 1
    fi
    sleep 5
  done
  echo "$svc is healthy"
done

if [ -s "$STATE" ]; then
  while read -r id; do
    [[ "$id" =~ ^[0-9A-Z]{26}$ ]] || continue
    post "/devices/$id/commands/resume"
    echo "resume sent to device $id (it records again at its next heartbeat, within about 15 s once it is online)"
  done <"$STATE"
  rm -f "$STATE"
fi

compose ps --format '{{.Service}}: {{.Status}}'
echo "Loopi is running again."
