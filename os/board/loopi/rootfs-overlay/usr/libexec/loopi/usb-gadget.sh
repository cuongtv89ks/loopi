#!/bin/sh
# Dev bring-up: when config.txt selects the dwc2 overlay, expose a composite USB device on the
# OTG port: ACM serial console (ttyGS0) + CDC-ECM network (usb0). In host mode the default
# dwc_otg driver owns the port, no device controller appears and this script does nothing.
set -eu
G=/sys/kernel/config/usb_gadget/loopi
UDC_WAIT_S=10

waited=0
while [ -z "$(ls /sys/class/udc 2>/dev/null)" ]; do
	waited=$((waited + 1))
	if [ "$waited" -gt "$UDC_WAIT_S" ]; then
		echo "no USB device controller: OTG port is in host mode"
		exit 0
	fi
	sleep 1
done
[ -d "$G" ] && exit 0

modprobe libcomposite
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
serial="$(awk '/^Serial/ {print $3}' /proc/cpuinfo)"
mac_tail="$(echo "$serial" | sed -E 's/.*(..)(..)(..)(..)(..)$/\1:\2:\3:\4:\5/')"

mkdir -p "$G"
cd "$G"
echo 0x1d6b > idVendor      # Linux Foundation
echo 0x0104 > idProduct     # Multifunction Composite Gadget
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB
echo 0xEF > bDeviceClass    # composite with an interface association (needed for ACM)
echo 0x02 > bDeviceSubClass
echo 0x01 > bDeviceProtocol
mkdir -p strings/0x409
echo "$serial" > strings/0x409/serialnumber
echo "Loopi" > strings/0x409/manufacturer
echo "Loopi dev console" > strings/0x409/product

mkdir -p configs/c.1/strings/0x409 functions/acm.usb0 functions/ecm.usb0
echo "ACM + ECM" > configs/c.1/strings/0x409/configuration
echo 250 > configs/c.1/MaxPower
# Stable MAC addresses so the computer keeps the same network interface across boots
echo "02:$mac_tail" > functions/ecm.usb0/dev_addr
echo "06:$mac_tail" > functions/ecm.usb0/host_addr
ln -s functions/acm.usb0 configs/c.1/
ln -s functions/ecm.usb0 configs/c.1/
ls /sys/class/udc | head -n 1 > UDC

systemctl --no-block start serial-getty@ttyGS0.service
echo "USB gadget ready: console ttyGS0, network usb0"
