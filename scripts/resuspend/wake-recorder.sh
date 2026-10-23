#!/bin/sh
# Records WHAT woke the phone from each suspend. Run as root, detached:
#   systemd-run --unit=fp5-wake-recorder --collect /bin/sh wake-recorder.sh [hours=12]
#   systemctl stop fp5-wake-recorder        # to stop
# Every second it snapshots /proc/interrupts + wakeup_sources; when the clock
# jumped (= phone was suspended) it logs the IRQ lines and wakeup sources that
# moved across the suspend (the first snapshot after resume is <1 s late, so the
# waking IRQ is in the list, together with a few that fire right after resume).
# Log: $K/wake-recorder.log  (small: only written on wakes)
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
K=${K:-/home/user/fp5-qtee-keep/resuspend-20261023}
H=${1:-12}
L=$K/wake-recorder.log
ws() { awk 'NR>1 {print $1, $2, $3, $4}' /sys/kernel/debug/wakeup_sources; }
echo "$(date -u +%T) recorder start (${H}h)" >> $L
p=$(mktemp); q=$(mktemp); wp=$(mktemp); wq=$(mktemp)
cp /proc/interrupts $p; ws > $wp
last=$(date +%s); end=$(( last + H*3600 ))
while [ $(date +%s) -lt $end ]; do
	sleep 1
	now=$(date +%s)
	cp /proc/interrupts $q; ws > $wq
	if [ $((now - last)) -gt 5 ]; then
		echo "$(date -u +%T) WAKE after ~$((now - last))s gap; bat=$(cat /sys/class/power_supply/qcom-battmgr-bat/capacity)% usb=$(cat /sys/class/power_supply/qcom-battmgr-usb/online 2>/dev/null) bl=$(cat /sys/class/backlight/*/bl_power)" >> $L
		echo " IRQs moved:" >> $L; sh $K/irqdiff.sh $p $q >> $L
		echo " wakeup sources changed (name active event wakeup):" >> $L
		diff $wp $wq | grep '^>' | sed 's/^>/  /' >> $L
	fi
	last=$now; mv $q $p; mv $wq $wp
done
rm -f $p $q $wp $wq
