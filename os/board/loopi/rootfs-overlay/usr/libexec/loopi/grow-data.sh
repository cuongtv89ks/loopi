#!/bin/sh
# Grow the data partition (p4) to the end of the SD card. Safe to re-run: every step is
# idempotent and the marker is written only after resize2fs succeeded.
set -eu
DISK=/dev/mmcblk0
PART=/dev/mmcblk0p4
MARKER=/data/.grown
[ -e "$MARKER" ] && exit 0
echo ", +" | sfdisk --no-reread --no-tell-kernel -N 4 "$DISK"
partx -u -n 4 "$DISK"
resize2fs "$PART"
touch "$MARKER"
