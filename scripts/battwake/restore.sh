#!/bin/sh
# Undo fp5-battwake (live, no reboot): remove rule/script/conf/s2idle drop-in,
# put qcom-battmgr-bat wakeup back to the kernel default (enabled) and
# mem_sleep back to deep.
set -u
rm -f /etc/udev/rules.d/90-fp5-battwake.rules /usr/local/sbin/fp5-battwake-apply /etc/fp5-battwake.conf \
	/etc/systemd/sleep.conf.d/fp5-battwake-s2idle.conf
udevadm control --reload 2>/dev/null || true
W=/sys/class/power_supply/qcom-battmgr-bat/power/wakeup
[ -f "$W" ] && echo enabled > "$W"
echo deep > /sys/power/mem_sleep
echo "qcom-battmgr-bat wakeup=$(cat $W 2>/dev/null) mem_sleep=$(cat /sys/power/mem_sleep)"
