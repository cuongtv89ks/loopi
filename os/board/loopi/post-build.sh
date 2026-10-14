#!/bin/sh
# Runs after the target tree is assembled, before the root filesystem image is made.
# The root filesystem is read-only, so units are enabled here rather than at runtime.
set -eu
WANTS="${TARGET_DIR}/etc/systemd/system/multi-user.target.wants"
mkdir -p "$WANTS"

enable_unit() {
	ln -sf "../$1" "$WANTS/$1"
}

enable_unit loopi-usbgadget.service
# root logs in on the USB gadget console the same way as on a serial console
if [ -f "${TARGET_DIR}/etc/securetty" ] && ! grep -qx ttyGS0 "${TARGET_DIR}/etc/securetty"; then
	echo ttyGS0 >> "${TARGET_DIR}/etc/securetty"
fi
