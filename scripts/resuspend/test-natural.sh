#!/bin/sh
# Natural unattended-wake test. Run as root, DETACHED (systemd-run) with all SSH
# sessions closed (an sshd "Active PAM session" block inhibitor stops any idle
# suspend and would invalidate the test).
#   test-natural.sh <tag> [alarm_s=540] [watch_s=1200]
# Arms an RTC alarm alarm_s seconds from now and does NOT suspend by itself:
# gsd-power suspends the idle phone (battery timeout 300 s), the RTC alarm then
# wakes it with nobody touching it, and we log whether it goes back to sleep on
# its own. Logs time gaps (= suspended), IRQ counters that moved across each
# suspend (= the wake source), and leaves no RTC alarm behind.
TAG=${1:-n}; ALM=${2:-540}; WATCH=${3:-1200}
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
D=/home/user/fp5-qtee-keep/resuspend-20261023
L=$D/natural-$TAG-$(date -u +%H%M%S).log
B=/sys/class/power_supply/qcom-battmgr-bat
T=/sys/kernel/tracing
ss() { cat /sys/power/suspend_stats/success; }
log() { echo "$(date -u +%T) $*" >> $L; }
state() { echo "ss=$(ss) bl=$(cat /sys/class/backlight/*/bl_power) locked=$(loginctl show-session c7 -p LockedHint --value) bat=$(cat $B/capacity)% $(cat $B/status) sshd_inhibit=$(systemd-inhibit --list --no-legend | grep -c sshd)"; }
echo 0 > $T/tracing_on; echo > $T/trace
for e in rtc/rtc_set_alarm rtc/rtc_timer_enqueue rtc/rtc_timer_fired alarmtimer/alarmtimer_suspend alarmtimer/alarmtimer_fired alarmtimer/alarmtimer_start power/wakeup_source_activate; do echo 1 > $T/events/$e/enable; done
echo 1 > $T/options/stacktrace; echo 1 > $T/tracing_on
( timeout $((ALM + WATCH + 120)) cat $T/trace_pipe > $D/trace-$TAG.txt ) &
( timeout $((ALM + WATCH + 120)) su user -c "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/10000/bus busctl --user monitor --match 'type=signal,interface=org.gnome.Mutter.IdleMonitor' --match 'type=signal,interface=org.gnome.ScreenSaver'" >> $D/idle-$TAG.txt 2>&1 ) &
log "start tag=$TAG alarm=${ALM}s watch=${WATCH}s; $(state)"
rtcwake -m no -s $ALM >> $L 2>&1
t0=$(date +%s); n0=$(ss); last=$t0; prev=$(mktemp); cp /proc/interrupts $prev
while [ $(( $(date +%s) - t0 )) -lt $((ALM + WATCH)) ]; do
	sleep 10
	now=$(date +%s)
	if [ $((now - last)) -gt 30 ]; then
		log "TIME GAP $((now - last))s (phone was suspended); IRQs that moved:"
		sh $D/irqdiff.sh $prev /proc/interrupts >> $L
	fi
	last=$now; cp /proc/interrupts $prev
	log "$(state)"
done
log "end: suspends=$(( $(ss) - n0 )); wakealarm=$(cat /sys/class/rtc/rtc0/wakealarm)"
echo 0 > /sys/class/rtc/rtc0/wakealarm
echo 0 > $T/tracing_on; echo 0 > $T/options/stacktrace
for e in rtc alarmtimer; do echo 0 > $T/events/$e/enable; done; echo 0 > $T/events/power/wakeup_source_activate
log "watcher: $(su user -c 'XDG_RUNTIME_DIR=/run/user/10000 systemctl --user is-active fp5-qtee-unlock') session=$(pidof fp5-qtee-session)"
rm -f $prev
