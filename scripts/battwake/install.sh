#!/bin/sh
# Install fp5-battwake on the phone (run on the phone from this directory).
#   sudo ./install.sh            # battmgr wakeup knob (udev rule + script + conf)
#   sudo ./install.sh --s2idle   # also switch systemd suspend to s2idle
# Backs up anything it would overwrite to $KEEP and writes $KEEP/restore.sh.
# No reboot. Applies live.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
KEEP=${KEEP:-/home/user/fp5-qtee-keep/battwake-$(date +%Y%m%d)}
mkdir -p "$KEEP/orig"
W=/sys/class/power_supply/qcom-battmgr-bat/power/wakeup
[ -e "$KEEP/orig/sysfs-state.txt" ] || { echo "mem_sleep $(cat /sys/power/mem_sleep)"; echo "bat_wakeup $(cat $W 2>/dev/null)"; } > "$KEEP/orig/sysfs-state.txt"
put() { # src dst mode
	if [ -e "$2" ] && [ ! -e "$KEEP/orig/$(basename "$2")" ]; then cp "$2" "$KEEP/orig/$(basename "$2")"; fi
	[ ! -e "$2" ] || [ -f "$2" ] || { echo "refusing: $2 is not a regular file" >&2; exit 1; }
	install -m "$3" "$1" "$2.new" && mv "$2.new" "$2"
	echo "installed $2"
}
put "$HERE/fp5-battwake-apply" /usr/local/sbin/fp5-battwake-apply 755
put "$HERE/fp5-battwake.conf" /etc/fp5-battwake.conf 644
put "$HERE/90-fp5-battwake.rules" /etc/udev/rules.d/90-fp5-battwake.rules 644
if [ "${1:-}" = --s2idle ]; then
	mkdir -p /etc/systemd/sleep.conf.d
	put "$HERE/fp5-battwake-s2idle.conf" /etc/systemd/sleep.conf.d/fp5-battwake-s2idle.conf 644
	echo s2idle > /sys/power/mem_sleep
fi
udevadm control --reload
udevadm trigger --action=change --subsystem-match=power_supply --sysname-match=qcom-battmgr-bat
udevadm settle -t 5 || true
cp "$HERE/restore.sh" "$KEEP/restore-battwake.sh" 2>/dev/null || true
echo "qcom-battmgr-bat wakeup=$(cat $W)  mem_sleep=$(cat /sys/power/mem_sleep)"
