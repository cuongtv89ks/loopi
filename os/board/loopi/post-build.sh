#!/bin/sh
# Runs after the target tree is assembled, before the root filesystem image is made.
# The root filesystem is read-only, so units are enabled here rather than at runtime.
set -eu
BOARD_DIR="$(cd "$(dirname "$0")" && pwd)"
: "${LOOPI_OS_VERSION:?set by os/build.sh}" "${LOOPI_VARIANT:?set by os/build.sh}" "${LOOPI_RAUC_KEYS:?set by os/build.sh}"
dev=no
[ -e "${TARGET_DIR}/etc/loopi-dev" ] && dev=yes
case "$LOOPI_VARIANT/$dev" in
	dev/yes | prod/no) ;;
	*) echo "post-build: LOOPI_VARIANT=$LOOPI_VARIANT but /etc/loopi-dev present=$dev (wrong output directory?)" >&2; exit 1 ;;
esac
WANTS="${TARGET_DIR}/etc/systemd/system/multi-user.target.wants"
mkdir -p "$WANTS"

enable_unit() {
	ln -sf "../$1" "$WANTS/$1"
}

if [ "$dev" = yes ]; then
	enable_unit loopi-usbgadget.service
	# root logs in on the USB gadget console the same way as on a serial console
	if [ -f "${TARGET_DIR}/etc/securetty" ] && ! grep -qx ttyGS0 "${TARGET_DIR}/etc/securetty"; then
		echo ttyGS0 >> "${TARGET_DIR}/etc/securetty"
	fi
fi

enable_unit loopi-growdata.service
# Mount points for the fstab entries: systemd cannot create them on the read-only root
mkdir -p "${TARGET_DIR}/boot" "${TARGET_DIR}/data"

enable_unit loopi-provision.service
enable_unit loopi-wifi.service
# ...and from the wlan0 device too (WantedBy= in its [Install]): after a brcmfmac reset re-creates wlan0, the device's
# Wants= starts WiFi again; BindsTo= had stopped it when the old wlan0 went away.
WLAN0_WANTS="${TARGET_DIR}/etc/systemd/system/sys-subsystem-net-devices-wlan0.device.wants"
mkdir -p "$WLAN0_WANTS"
ln -sf ../loopi-wifi.service "$WLAN0_WANTS/loopi-wifi.service"
enable_unit loopi-agent.service

# Dev access: the root filesystem is read-only, so SSH host keys and authorized_keys live on /data.
if [ "$dev" = yes ]; then
	rm -rf "${TARGET_DIR}/etc/dropbear" "${TARGET_DIR}/root/.ssh"
	ln -s /data/dev/dropbear "${TARGET_DIR}/etc/dropbear"
	mkdir -p "${TARGET_DIR}/root"
	ln -s /data/dev/ssh "${TARGET_DIR}/root/.ssh"
	ln -sf /usr/lib/systemd/system/dropbear.service "$WANTS/dropbear.service"
	enable_unit loopi-devkeys.service
fi

# P4a: which system this is; the agent reports it in every heartbeat (contract OsVersion).
printf 'LOOPI_OS_VERSION=%s\nLOOPI_OS_VARIANT=%s\n' "$LOOPI_OS_VERSION" "$LOOPI_VARIANT" > "${TARGET_DIR}/etc/loopi-release"

# P4a: OTA. The trial-boot guard, the RAUC keyring (the CA certificate only, never a key) and this slot's boot files,
# which loopi-boot install-files copies to /boot/a/ or /boot/b/ (P4a design §4).
TIMERS="${TARGET_DIR}/etc/systemd/system/timers.target.wants"
mkdir -p "$TIMERS"
ln -sf ../loopi-tryboot-guard.timer "$TIMERS/loopi-tryboot-guard.timer"
install -D -m 0644 "$LOOPI_RAUC_KEYS/ca.cert.pem" "${TARGET_DIR}/etc/rauc/keyring.pem"
SHARE="${TARGET_DIR}/usr/share/loopi/boot"
rm -rf "$SHARE"
mkdir -p "$SHARE"
cp "${BINARIES_DIR}/zImage" "${BINARIES_DIR}/bcm2708-rpi-zero-w.dtb" "$SHARE/"
cp -r "${BINARIES_DIR}/rpi-firmware/overlays" "$SHARE/overlays"
# The firmware loads ${os_prefix}overlays/ only when that directory holds a README (Buildroot's rpi-firmware package
# installs it next to the overlays); without it the slot would boot with no overlays at all, so stop here.
if [ ! -f "$SHARE/overlays/README" ]; then
	echo "post-build: ${BINARIES_DIR}/rpi-firmware/overlays/README is missing; the firmware would ignore a/overlays and b/overlays" >&2
	exit 1
fi
cp "${BOARD_DIR}/config.txt" "$SHARE/loopi-config.txt"
if [ "$dev" = yes ]; then cat "${BOARD_DIR}/config-dev.txt" >> "$SHARE/loopi-config.txt"; fi
cp "${BOARD_DIR}/cmdline.txt" "$SHARE/cmdline.txt"
case "${LOOPI_BROKEN:-}" in
	"") ;;
	agent)  # no agent, so no heartbeat: the trial must roll back after 10 minutes
		printf '#!/bin/sh\n# Deliberately broken acceptance build (P4a design section 13).\nexit 1\n' \
			> "${TARGET_DIR}/usr/libexec/loopi/run-agent"
		chmod 0755 "${TARGET_DIR}/usr/libexec/loopi/run-agent" ;;
	kernel)  # p4 is ext4, not squashfs: the kernel panics and panic=10 reboots into the good slot
		sed -i 's|root=@ROOT@|root=/dev/mmcblk0p4|' "$SHARE/cmdline.txt" ;;
	*) echo "post-build: unknown LOOPI_BROKEN=$LOOPI_BROKEN" >&2; exit 1 ;;
esac
