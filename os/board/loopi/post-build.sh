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

enable_unit loopi-growdata.service
# Mount points for the fstab entries: systemd cannot create them on the read-only root
mkdir -p "${TARGET_DIR}/boot" "${TARGET_DIR}/data"

enable_unit loopi-provision.service
enable_unit loopi-wifi.service
enable_unit loopi-agent.service

# Dev access: the root filesystem is read-only, so SSH host keys and authorized_keys live on /data.
if [ -e "${TARGET_DIR}/etc/loopi-dev" ]; then
	rm -rf "${TARGET_DIR}/etc/dropbear" "${TARGET_DIR}/root/.ssh"
	ln -s /data/dev/dropbear "${TARGET_DIR}/etc/dropbear"
	mkdir -p "${TARGET_DIR}/root"
	ln -s /data/dev/ssh "${TARGET_DIR}/root/.ssh"
	ln -sf /usr/lib/systemd/system/dropbear.service "$WANTS/dropbear.service"
	enable_unit loopi-devkeys.service
fi
