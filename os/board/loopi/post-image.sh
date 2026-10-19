#!/bin/sh
# Assemble the SD card (P4a design §4): boot (FAT: firmware, config.txt booting slot A, slot dirs a/ and b/), rootfs-A
# (squashfs), rootfs-B (empty until the first update), data (ext4). Then the signed RAUC bundle of this rootfs and the
# description the brain lists it by (P4a design §8).
set -eu
BOARD_DIR="$(cd "$(dirname "$0")" && pwd)"
: "${LOOPI_OS_VERSION:?}" "${LOOPI_VARIANT:?}" "${LOOPI_COMMIT:?}" "${LOOPI_RAUC_KEYS:?}"
NAME="loopi-${LOOPI_VARIANT}-${LOOPI_OS_VERSION}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# host-dosfstools installs mkfs.vfat under sbin; accept bin as well, so a packaging change does not cost a 1-3 h build
MKFS_VFAT="${HOST_DIR}/sbin/mkfs.vfat"
[ -x "$MKFS_VFAT" ] || MKFS_VFAT="${HOST_DIR}/bin/mkfs.vfat"
[ -x "$MKFS_VFAT" ] || { echo "post-image: no mkfs.vfat in ${HOST_DIR}/sbin or ${HOST_DIR}/bin" >&2; exit 1; }

# --- boot partition, laid out by the shipped loopi-boot, so the first config.txt is the one an update would write
STAGE="$WORK/boot"
mkdir -p "$STAGE"
cp "${BINARIES_DIR}/rpi-firmware/bootcode.bin" "${BINARIES_DIR}/rpi-firmware/start.elf" \
	"${BINARIES_DIR}/rpi-firmware/fixup.dat" "${BOARD_DIR}/loopi.toml.example" "$STAGE/"
cp "${TARGET_DIR}/usr/share/loopi/boot/loopi-config.txt" "$STAGE/config.txt"  # USB mode = the template's own
LOOPI_BOOT_DIR="$STAGE" LOOPI_STATE_DIR="$WORK/state" LOOPI_CMDLINE=/dev/null LOOPI_REMOUNT=0 \
	sh "${TARGET_DIR}/usr/libexec/loopi/loopi-boot" install-files A "${TARGET_DIR}"
mv "$STAGE/tryboot.txt" "$STAGE/config.txt"
BOOT_IMG="${BINARIES_DIR}/boot.vfat"
rm -f "$BOOT_IMG"
# FAT32 with 512-byte clusters: 64 MiB is just big enough for a FAT32 without mkdosfs warning about too few clusters
# (the default would silently pick FAT16).
"$MKFS_VFAT" -F 32 -s 1 -n BOOT -C "$BOOT_IMG" 65536
MTOOLS_SKIP_CHECK=1 "${HOST_DIR}/bin/mcopy" -s -i "$BOOT_IMG" "$STAGE"/* ::
MTOOLS_SKIP_CHECK=1 "${HOST_DIR}/bin/mmd" -i "$BOOT_IMG" ::b

# --- data partition (empty; grows on first boot)
DATA_IMG="${BINARIES_DIR}/data.ext4"
EMPTY_DIR="$WORK/empty"
mkdir -p "$EMPTY_DIR"
rm -f "${DATA_IMG}"
truncate -s 64M "${DATA_IMG}"
"${HOST_DIR}/sbin/mkfs.ext4" -q -F -L data -b 4096 -i 65536 -d "${EMPTY_DIR}" "${DATA_IMG}"

support/scripts/genimage.sh -c "${BOARD_DIR}/genimage.cfg"

# --- signed update bundle (spec D26-D28) and its description for the brain
BUNDLE_DIR="$WORK/bundle"
mkdir -p "$BUNDLE_DIR"
cp "${BINARIES_DIR}/rootfs.squashfs" "$BUNDLE_DIR/rootfs.squashfs"
cat > "$BUNDLE_DIR/manifest.raucm" <<EOF
[update]
compatible=loopi-rpi0w
version=${LOOPI_OS_VERSION}

[bundle]
format=verity

[image.rootfs]
filename=rootfs.squashfs
EOF
BUNDLE="${BINARIES_DIR}/${NAME}.raucb"
rm -f "${BINARIES_DIR}"/loopi-*.raucb "${BINARIES_DIR}"/loopi-*.json
"${HOST_DIR}/bin/rauc" bundle --cert="${LOOPI_RAUC_KEYS}/signing.cert.pem" --key="${LOOPI_RAUC_KEYS}/signing.key.pem" \
	"$BUNDLE_DIR" "$BUNDLE"
"${HOST_DIR}/bin/rauc" info --keyring="${LOOPI_RAUC_KEYS}/ca.cert.pem" "$BUNDLE" > /dev/null
SHA="$(sha256sum "$BUNDLE" | cut -d' ' -f1)"
SIZE="$(stat -c %s "$BUNDLE")"
printf '{"bundle": "%s.raucb", "version": "%s", "variant": "%s", "sha256": "%s", "size": %s, "built_at": "%s", "commit": "%s"}\n' \
	"$NAME" "$LOOPI_OS_VERSION" "$LOOPI_VARIANT" "$SHA" "$SIZE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LOOPI_COMMIT" \
	> "${BINARIES_DIR}/${NAME}.json"
