#!/bin/sh
# Assemble the SD card: boot (FAT), rootfs-A (squashfs), rootfs-B (empty), data (ext4).
set -eu
BOARD_DIR="$(cd "$(dirname "$0")" && pwd)"

cp "${BOARD_DIR}/cmdline.txt" "${BINARIES_DIR}/rpi-firmware/cmdline.txt"
cp "${BOARD_DIR}/loopi.toml.example" "${BINARIES_DIR}/loopi.toml.example"

DATA_IMG="${BINARIES_DIR}/data.ext4"
EMPTY_DIR="$(mktemp -d)"
rm -f "${DATA_IMG}"
truncate -s 64M "${DATA_IMG}"
"${HOST_DIR}/sbin/mkfs.ext4" -q -F -L data -b 4096 -i 65536 -d "${EMPTY_DIR}" "${DATA_IMG}"
rmdir "${EMPTY_DIR}"

support/scripts/genimage.sh -c "${BOARD_DIR}/genimage.cfg"
