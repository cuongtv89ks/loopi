#!/usr/bin/env bash
# Build the Loopi dev or prod image (or only the agent) with Buildroot inside Docker.
# Usage: [VARIANT=dev|prod] [LOOPI_BROKEN=agent|kernel] os/build.sh [image|agent|shell]      Output: os/out/
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
TARGET="${1:-image}"
BUILDROOT_REF="${BUILDROOT_REF:-2026.02}"   # tag, not the 2026.02.x branch (point releases ship stale linux-headers hash dirs)
BUILDER_IMAGE=loopi-os-builder
VOLUME=loopi-buildroot   # named volume: case-sensitive and fast on macOS

mkdir -p "$HERE/out"

# Variant (spec §6.5) and the OS version every build stamps (P4a design §2): <UTC date>-<7-char commit>, "-dirty" when
# the tree has uncommitted changes (publish-bundle.sh refuses those), "-broken-<what>" for the deliberately broken
# acceptance bundles (LOOPI_BROKEN=agent|kernel, P4a design §13).
VARIANT="${VARIANT:-dev}"
case "$VARIANT" in dev | prod) ;; *) echo "VARIANT must be dev or prod, not '$VARIANT'" >&2; exit 2 ;; esac
case "${LOOPI_BROKEN:-}" in "" | agent | kernel) ;; *) echo "LOOPI_BROKEN must be agent or kernel" >&2; exit 2 ;; esac
COMMIT="$(git -C "$REPO" rev-parse HEAD)"
VERSION="$(date -u +%Y.%m.%d)-$(git -C "$REPO" rev-parse --short=7 HEAD)"
[ -z "$(git -C "$REPO" status --porcelain)" ] || VERSION="$VERSION-dirty"
[ -z "${LOOPI_BROKEN:-}" ] || VERSION="$VERSION-broken-$LOOPI_BROKEN"
# OTA signing keys (os/scripts/rauc-keys.sh): images carry the CA certificate and bundles are signed. Never in git.
KEYS="${LOOPI_RAUC_KEYS:-$HOME/.config/loopi/rauc}"
key_mount=()
if [ "$TARGET" = image ]; then
  for f in ca.cert.pem signing.cert.pem signing.key.pem; do
    [ -f "$KEYS/$f" ] || { echo "missing $KEYS/$f: create the OTA signing keys with os/scripts/rauc-keys.sh" >&2; exit 2; }
  done
  key_mount=(-v "$KEYS:/keys:ro")
fi

docker build -q -t "$BUILDER_IMAGE" "$HERE/docker" >/dev/null
docker volume create "$VOLUME" >/dev/null
docker run --rm -t \
  -e BUILDROOT_REF="$BUILDROOT_REF" -e TARGET="$TARGET" -e VARIANT="$VARIANT" \
  -e LOOPI_VARIANT="$VARIANT" -e LOOPI_OS_VERSION="$VERSION" -e LOOPI_COMMIT="$COMMIT" \
  -e LOOPI_BROKEN="${LOOPI_BROKEN:-}" -e LOOPI_RAUC_KEYS=/keys \
  "${key_mount[@]}" \
  -v "$VOLUME:/br" -v "$REPO:/src:ro" -v "$HERE/out:/out" \
  "$BUILDER_IMAGE" /src/os/scripts/container-build.sh
