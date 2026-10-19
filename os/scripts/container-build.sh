#!/usr/bin/env bash
# Runs inside the builder container; re-executes itself as the unprivileged "builder" user.
set -euo pipefail
if [ "$(id -u)" = 0 ]; then
  chown builder:builder /br /out
  exec runuser -u builder -- "$0" "$@"
fi
BR=/br/buildroot
SRC=/br/src
# One output directory per variant: Buildroot does not rebuild correctly when fragments change under an existing one.
# Dev keeps /br/output (built since P1).
case "${VARIANT:-dev}" in
  dev) OUT=/br/output ;;
  prod) OUT=/br/output-prod ;;
  *) echo "unknown VARIANT ${VARIANT}" >&2; exit 2 ;;
esac

# Copy the repo without host build artefacts (Buildroot rsyncs agent/ for the local package).
rsync -a --delete --exclude target --exclude .venv --exclude out --exclude .git /src/ "$SRC/"
[ -d "$BR" ] || git clone --depth 1 --branch "$BUILDROOT_REF" https://gitlab.com/buildroot.org/buildroot.git "$BR"

# BUILDROOT_REF only selects the checkout on the very first clone. An existing checkout and the
# build output made from it are never switched or invalidated, so refuse a mismatch instead of
# quietly building a different Buildroot than the one asked for. Nothing is deleted here.
have_ref="$(git -C "$BR" symbolic-ref -q --short HEAD 2>/dev/null \
  || git -C "$BR" describe --tags --exact-match 2>/dev/null || echo unknown)"
if [ "$have_ref" != "$BUILDROOT_REF" ]; then
  cat >&2 <<EOF
error: the Buildroot checkout in the build volume is '$have_ref', but BUILDROOT_REF is '$BUILDROOT_REF'.
An existing checkout is never switched (and /br/output would silently stay built from the old one),
so nothing was built. Either:
  - keep building what the volume has:  BUILDROOT_REF=$have_ref os/build.sh ...
  - or switch to '$BUILDROOT_REF': this DELETES the Buildroot checkout, the downloads and all build
    output, and the next build starts from scratch (1-3 h). Remove the volume explicitly, then rebuild:
        docker volume rm loopi-buildroot
EOF
  exit 3
fi

configure() {
  make -C "$BR" O="$OUT" BR2_EXTERNAL="$SRC/os" raspberrypi0w_defconfig
  "$BR/support/kconfig/merge_config.sh" -m -O "$OUT" "$OUT/.config" \
    "$SRC/os/configs/loopi.fragment" "$SRC/os/configs/$VARIANT.fragment"
  make -C "$BR" O="$OUT" olddefconfig
  "$SRC/os/scripts/verify-config.sh" "$OUT/.config" "$SRC/os/configs/loopi.fragment" "$SRC/os/configs/$VARIANT.fragment"
}

# Buildroot does not notice a changed kernel config fragment: reconfigure the kernel when ours changed since the last
# successful image build of this output directory.
LINUX_STAMP="$OUT/.loopi-linux-fragment.sha256"
linux_fragment_sha() { sha256sum "$SRC/os/board/loopi/linux.fragment" | cut -d' ' -f1; }

case "$TARGET" in
  image)
    configure
    if ls -d "$OUT"/build/linux-* >/dev/null 2>&1 && [ "$(cat "$LINUX_STAMP" 2>/dev/null)" != "$(linux_fragment_sha)" ]; then
      make -C "$BR" O="$OUT" linux-reconfigure
    fi
    make -C "$BR" O="$OUT"
    linux_fragment_sha > "$LINUX_STAMP"
    NAME="loopi-$VARIANT-$LOOPI_OS_VERSION"
    # The deliberately broken acceptance builds (LOOPI_BROKEN) fail the structure checks by design.
    [ -n "${LOOPI_BROKEN:-}" ] || "$SRC/os/tests/check-image.sh" "$OUT/images"
    cp "$OUT/images/sdcard.img" "/out/$NAME.img"
    cp "$OUT/images/rootfs.squashfs" "/out/$NAME.squashfs"
    cp "$OUT/images/$NAME.raucb" "$OUT/images/$NAME.json" /out/
    ;;
  agent)
    make -C "$BR" O="$OUT" loopi-agent-rebuild
    cp "$OUT/target/usr/bin/loopi-agent" /out/loopi-agent
    ;;
  shell)
    exec bash
    ;;
  *)
    echo "unknown target: $TARGET (use image | agent | shell)" >&2
    exit 2
    ;;
esac
