#!/usr/bin/env bash
# End-to-end check of a running brain: pairing, mTLS upload, duplicate, spoofing, heartbeat, web auth,
# the same stack reached through an IP-literal URL (no TLS SNI), which is how devices are configured,
# and the pipeline worker turning an uploaded test video into keyframes. It removes everything it created.
set -euo pipefail
cd "$(dirname "$0")"

if [[ -z "${LOOPI_DATA_DIR:-}" && -f .env ]]; then
  LOOPI_DATA_DIR="$(sed -n 's/^LOOPI_DATA_DIR=//p' .env | tail -n1 | tr -d "'\"\r")"
fi
BASE="${LOOPI_SMOKE_URL:-https://localhost:8443}"
PKI="${LOOPI_DATA_DIR:-/srv/loopi}/pki"
# IP-literal URLs send no TLS SNI (RFC 6066); devices and browsers use https://<LAN IP>:8443, so smoke checks that too.
# LOOPI_SMOKE_IP must be an IPv4 address listed in the server cert (README step 3); default: the first LAN address.
if [[ -z "${LOOPI_SMOKE_IP:-}" ]]; then
  LOOPI_SMOKE_IP="$(hostname -I 2>/dev/null | cut -d' ' -f1 || true)"
  [[ -n "$LOOPI_SMOKE_IP" ]] || LOOPI_SMOKE_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"  # macOS
fi
if ! [[ "$LOOPI_SMOKE_IP" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
  echo "FAIL: LOOPI_SMOKE_IP must be an IPv4 address (got '${LOOPI_SMOKE_IP}'); set it to the PC's LAN IP" >&2
  exit 1
fi
IP_BASE="https://$LOOPI_SMOKE_IP:8443"
# How long the worker may take to analyse the test video; behind a backlog it needs longer (README step 6).
SMOKE_ANALYSIS_TIMEOUT="${SMOKE_ANALYSIS_TIMEOUT:-90}"
if ! [[ "$SMOKE_ANALYSIS_TIMEOUT" =~ ^[0-9]+$ ]]; then
  echo "FAIL: SMOKE_ANALYSIS_TIMEOUT must be a number of seconds (got '$SMOKE_ANALYSIS_TIMEOUT')" >&2
  exit 1
fi
WORK="$(mktemp -d)"
FP=""         # sha256 of the smoke device's certificate, set as soon as the device is paired
DEVICE_ID=""  # set once the smoke device's id is looked up; from then on every exit removes what it created

remove_smoke_data() {  # delete the smoke device's rows and media files (revoking it is revoke_smoke_device's job)
  [[ "$DEVICE_ID" =~ ^[0-9A-HJKMNP-TV-Z]{26}$ ]] || return 0  # never delete anything for a malformed id
  # Moments and items of that device only (keyframes and transcripts go with their items).
  docker compose exec -T db psql -U loopi -d loopi -v ON_ERROR_STOP=1 -q -c \
    "DELETE FROM moment WHERE device_id = '$DEVICE_ID'; DELETE FROM item WHERE device_id = '$DEVICE_ID';" >/dev/null &&
  # Paths inside the api container (LOOPI_MEDIA_ROOT).
  docker compose exec -T api rm -rf "/srv/loopi/media/raw/$DEVICE_ID" "/srv/loopi/media/view/$DEVICE_ID" \
    "/srv/loopi/media/keyframes/$DEVICE_ID"
}

revoke_smoke_device() {  # idempotent: by id when the id is known, else by certificate fingerprint; never by anything malformed
  local where
  if [[ "$DEVICE_ID" =~ ^[0-9A-HJKMNP-TV-Z]{26}$ ]]; then
    where="id = '$DEVICE_ID'"
  elif [[ "$FP" =~ ^[0-9a-f]{64}$ ]]; then
    where="cert_fingerprint = '$FP'"
  else
    return 0  # the device does not exist yet
  fi
  docker compose exec -T db psql -U loopi -d loopi -v ON_ERROR_STOP=1 -q -c \
    "UPDATE device SET revoked = TRUE WHERE $where" >/dev/null
}

on_exit() {  # runs on every exit, passed or failed; keeps the exit status
  local status=$?
  # Revoking never waits for the deletion: the api container may be the very thing that is down.
  remove_smoke_data || echo "WARN: test data of smoke device $DEVICE_ID was not fully removed" >&2
  revoke_smoke_device || echo "WARN: the smoke device was not revoked (fingerprint ${FP:-unknown}); revoke it on the Devices page" >&2
  rm -rf "$WORK"
  exit "$status"
}
trap on_exit EXIT

sha256_hex() {  # reads stdin, prints lowercase hex sha256 (Linux and macOS)
  if command -v sha256sum >/dev/null; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi
}

expect() {  # expect <http-code> <curl args...>
  local want="$1"; shift
  local got
  got="$(curl -s -o "$WORK/body" -w '%{http_code}' --cacert "$PKI/ca.crt" "$@")"
  if [[ "$got" != "$want" ]]; then
    echo "FAIL: expected $want, got $got for: $*" >&2
    cat "$WORK/body" >&2
    exit 1
  fi
}

# Wait (max 60 s) until Caddy reaches a migrated api: an unauthenticated device call answers 401, not 502/refused.
for _ in $(seq 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --cacert "$PKI/ca.crt" "$BASE/v1/heartbeat" || true)"
  [[ "$code" == "401" || "$code" == "405" ]] && break
  sleep 2
done
if [[ "$code" != "401" && "$code" != "405" ]]; then echo "FAIL: stack not ready (last HTTP code $code)" >&2; exit 1; fi

# No-SNI checks first, before any device exists: the server cert must list the IP, then both route families
# (device API without a client cert -> 401 from the api, web UI -> basic-auth challenge) must answer through it.
if ! openssl x509 -in "$PKI/server.crt" -noout -ext subjectAltName | grep -qF "IP Address:$LOOPI_SMOKE_IP"; then
  echo "FAIL: $PKI/server.crt has no IP SAN $LOOPI_SMOKE_IP (re-run init-pki with --ip, or set LOOPI_SMOKE_IP)" >&2
  exit 1
fi
expect 401 -X POST -H 'Content-Type: application/json' --data '{}' "$IP_BASE/v1/heartbeat"
expect 401 "$IP_BASE/"

TOKEN="$(docker compose exec -T api loopi-brain pair-token | head -n1 | tr -d '\r')"
openssl ecparam -name prime256v1 -genkey -noout -out "$WORK/device.key"
openssl req -new -key "$WORK/device.key" -subj "/CN=smoke" -out "$WORK/device.csr"
python3 - "$TOKEN" "$WORK/device.csr" > "$WORK/pair.json" <<'PY'
import json, sys
print(json.dumps({"token": sys.argv[1], "csr_pem": open(sys.argv[2]).read(),
                  "hw_info": {"model": "smoke", "serial": "0", "agent_version": "0.1.0"}}))
PY
expect 201 -H 'Content-Type: application/json' --data @"$WORK/pair.json" "$BASE/v1/pair"
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["cert_pem"], end="")' "$WORK/body" > "$WORK/device.crt"
FP="$(openssl x509 -in "$WORK/device.crt" -outform DER | sha256_hex)"
MTLS=(--cert "$WORK/device.crt" --key "$WORK/device.key")
DEVICE_ID="$(docker compose exec -T db psql -U loopi -d loopi -tAc \
  "SELECT id FROM device WHERE cert_fingerprint = '$FP'" | head -n1 | tr -d '\r')"
if ! [[ "$DEVICE_ID" =~ ^[0-9A-HJKMNP-TV-Z]{26}$ ]]; then
  echo "FAIL: smoke device not found (got '$DEVICE_ID')" >&2
  exit 1  # not a usable id: the exit trap revokes the device by its fingerprint
fi

new_ulid() {
  python3 -c 'import secrets,time
A="0123456789ABCDEFGHJKMNPQRSTVWXYZ"; t=int(time.time()*1000)
print("".join(A[(t>>(5*i))&31] for i in range(9,-1,-1))+"".join(secrets.choice(A) for _ in range(16)))'
}
ID="$(new_ulid)"
printf '{"note":"smoke"}' > "$WORK/marker.json"
SHA="$(sha256_hex < "$WORK/marker.json")"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ITEM=(-X PUT -H "Content-Type: application/json" -H "X-Loopi-Kind: marker"
      -H "X-Loopi-Start: $NOW" -H "X-Loopi-End: $NOW" -H "X-Loopi-Sha256: $SHA"
      --data-binary @"$WORK/marker.json")

expect 201 "${MTLS[@]}" "${ITEM[@]}" "$BASE/v1/items/$ID"
expect 200 "${MTLS[@]}" "${ITEM[@]}" "$BASE/v1/items/$ID"
# Spoof: no client cert, forged fingerprint of the real device -> must be rejected.
expect 401 -H "X-Loopi-Client-Fingerprint: $FP" "${ITEM[@]}" "$BASE/v1/items/$ID"

HB='{"agent_version":"0.1.0","uptime_s":1,"disk_used_pct":1.0,"queue_depth":0,"clock_synced":true,"paused":false,"audio_enabled":true,"recording_video":false,"recording_audio":false,"acked_command_ids":[]}'
expect 200 "${MTLS[@]}" -H 'Content-Type: application/json' --data "$HB" "$BASE/v1/heartbeat"
expect 401 "$BASE/"
# Same device through the IP-literal URL: the client-cert fingerprint must reach the api without SNI too.
expect 200 "${MTLS[@]}" -H 'Content-Type: application/json' --data "$HB" "$IP_BASE/v1/heartbeat"
# Pipeline: a 5 s test video (made by the api image's ffmpeg) must be analysed by the worker within
# SMOKE_ANALYSIS_TIMEOUT seconds. Keyframe extraction needs no AI model. The picture is static colour bars:
# an animated pattern moves too much between frames and the keyframe selection rejects every frame as blurred.
docker compose exec -T api ffmpeg -nostdin -loglevel error -f lavfi -i smptebars=size=320x240:rate=2 \
  -frames:v 10 -c:v libx264 -g 4 -bsf:v h264_mp4toannexb -f h264 - > "$WORK/video.h264"
VID="$(new_ulid)"
VSHA="$(sha256_hex < "$WORK/video.h264")"
expect 201 "${MTLS[@]}" -X PUT -H "Content-Type: video/h264" -H "X-Loopi-Kind: video" -H "X-Loopi-Video-Fps: 2" \
  -H "X-Loopi-Start: $NOW" -H "X-Loopi-End: $NOW" -H "X-Loopi-Sha256: $VSHA" \
  --data-binary @"$WORK/video.h264" "$BASE/v1/items/$VID"
STATE=""
DEADLINE=$((SECONDS + SMOKE_ANALYSIS_TIMEOUT))
while (( SECONDS < DEADLINE )); do
  STATE="$(docker compose exec -T db psql -U loopi -d loopi -tAc \
    "SELECT analysis_state FROM item WHERE id = '$VID'" | tr -d '\r')"
  [[ "$STATE" == "done" || "$STATE" == "skipped" ]] && break  # both are final
  sleep 2
done
if [[ "$STATE" == "skipped" ]]; then
  echo "FAIL: the worker skipped the test video (its raw file was missing or marked unusable); see: docker compose logs worker" >&2
  exit 1
elif [[ "$STATE" != "done" ]]; then
  echo "FAIL: test video not analysed within $SMOKE_ANALYSIS_TIMEOUT s (state '$STATE');" \
    "a backlog delays it (raise SMOKE_ANALYSIS_TIMEOUT); see: docker compose logs worker" >&2
  exit 1
fi
KEYFRAMES="$(docker compose exec -T db psql -U loopi -d loopi -tAc \
  "SELECT count(*) FROM keyframe WHERE item_id = '$VID'" | tr -d '\r')"
[[ "$KEYFRAMES" -ge 1 ]] || { echo "FAIL: test video produced no keyframe" >&2; exit 1; }
docker compose exec -T worker loopi-brain worker-health >/dev/null \
  || { echo "FAIL: the worker sent no heartbeat in the last 60 s" >&2; exit 1; }
# Leave nothing behind (the exit trap does the same after a failure), then check the revocation.
remove_smoke_data
revoke_smoke_device
REMOVED="$DEVICE_ID"; REMOVED_FP="$FP"
DEVICE_ID=""; FP=""  # all removed and revoked: the exit trap has nothing left to do
expect 401 "${MTLS[@]}" -H 'Content-Type: application/json' --data "$HB" "$BASE/v1/heartbeat"
echo "Removed the smoke data and revoked smoke device $REMOVED"
echo "OK: smoke test passed (device fingerprint $REMOVED_FP)"
