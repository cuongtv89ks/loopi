#!/usr/bin/env bash
# Create the OTA signing keys once (P4a design §9): a CA, whose certificate every image carries as the RAUC keyring,
# and a signing certificate issued by it for os/build.sh to sign bundles with. Kept outside git and never printed.
# Back the directory up: without it no update can ever be installed again, only a re-flash.
# Usage: os/scripts/rauc-keys.sh            (LOOPI_RAUC_KEYS overrides ~/.config/loopi/rauc)
set -euo pipefail
DIR="${LOOPI_RAUC_KEYS:-$HOME/.config/loopi/rauc}"
umask 077
if [ -e "$DIR/ca.key.pem" ] || [ -e "$DIR/signing.key.pem" ]; then
  echo "OTA signing keys already exist in $DIR; nothing changed" >&2
  exit 1
fi
mkdir -p "$DIR"
chmod 700 "$DIR"
openssl ecparam -name prime256v1 -genkey -noout -out "$DIR/ca.key.pem"
openssl req -x509 -new -key "$DIR/ca.key.pem" -sha256 -days 7300 -subj "/CN=Loopi OTA CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -out "$DIR/ca.cert.pem"
openssl ecparam -name prime256v1 -genkey -noout -out "$DIR/signing.key.pem"
openssl req -new -key "$DIR/signing.key.pem" -subj "/CN=Loopi OTA signing" -out "$DIR/signing.csr.pem"
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n' \
  > "$DIR/signing.ext"
openssl x509 -req -in "$DIR/signing.csr.pem" -CA "$DIR/ca.cert.pem" -CAkey "$DIR/ca.key.pem" -CAcreateserial \
  -days 7300 -sha256 -extfile "$DIR/signing.ext" -out "$DIR/signing.cert.pem" 2>/dev/null
rm -f "$DIR/signing.csr.pem" "$DIR/signing.ext" "$DIR/ca.cert.srl"
chmod 600 "$DIR"/*
openssl verify -CAfile "$DIR/ca.cert.pem" "$DIR/signing.cert.pem" > /dev/null
echo "Created the OTA signing keys in $DIR. Back this directory up: without it no update can be installed."
