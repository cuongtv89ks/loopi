#!/usr/bin/env bash
# End-to-end check of a running brain: pairing, mTLS upload, duplicate, spoofing, heartbeat, web auth,
# and the same stack reached through an IP-literal URL (no TLS SNI), which is how devices are configured.
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
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

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

ID="$(python3 -c 'import secrets,time
A="0123456789ABCDEFGHJKMNPQRSTVWXYZ"; t=int(time.time()*1000)
print("".join(A[(t>>(5*i))&31] for i in range(9,-1,-1))+"".join(secrets.choice(A) for _ in range(16)))')"
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
# Leave no test device behind: revoke the smoke device (a revoked device is rejected from now on).
DEVICE_ID="$(docker compose exec -T db psql -U loopi -d loopi -tAc \
  "UPDATE device SET revoked = TRUE WHERE cert_fingerprint = '$FP' RETURNING id" | head -n1 | tr -d '\r')"
[[ -n "$DEVICE_ID" ]] || { echo "FAIL: could not revoke smoke device" >&2; exit 1; }
expect 401 "${MTLS[@]}" -H 'Content-Type: application/json' --data "$HB" "$BASE/v1/heartbeat"
echo "Revoked smoke device $DEVICE_ID"
echo "OK: smoke test passed (device fingerprint $FP)"
