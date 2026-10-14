#!/usr/bin/env bash
# Build the Loopi dev image (or only the agent) with Buildroot inside Docker.
# Usage: os/build.sh [image|agent|shell]      Output: os/out/
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
TARGET="${1:-image}"
BUILDROOT_REF="${BUILDROOT_REF:-2026.02}"   # tag, not the 2026.02.x branch (point releases ship stale linux-headers hash dirs)
BUILDER_IMAGE=loopi-os-builder
VOLUME=loopi-buildroot   # named volume: case-sensitive and fast on macOS

mkdir -p "$HERE/out"
docker build -q -t "$BUILDER_IMAGE" "$HERE/docker" >/dev/null
docker volume create "$VOLUME" >/dev/null
docker run --rm -t \
  -e BUILDROOT_REF="$BUILDROOT_REF" -e TARGET="$TARGET" \
  -v "$VOLUME:/br" -v "$REPO:/src:ro" -v "$HERE/out:/out" \
  "$BUILDER_IMAGE" /src/os/scripts/container-build.sh
