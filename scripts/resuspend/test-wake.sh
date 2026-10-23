#!/bin/sh
# Controlled unattended-wake test. Run as root, detached (systemd-run), SSH closed.
#   test-wake.sh <tag> [sleep_s=60] [watch_s=480]
# Suspends with an RTC alarm, then logs every 10 s whether the phone went back
# to sleep on its own. Leaves no rtcwake alarm behind.
TAG=${1:-t}; SLP=${2:-60}; WATCH=${3:-480}
D=/home/user/fp5-qtee-keep/resuspend-20261023
L=$D/test-$TAG-$(date -u +%H%M%S).log
B=/sys/class/power_supply/qcom-battmgr-bat
U=10000
BUS=unix:path=/run/user/$U/bus
ss() { cat /sys/power/suspend_stats/success; }
log() { echo "$(date -u +%T) $*" >> $L; }
rtcws() { grep rtc@6100 /sys/kernel/debug/wakeup_sources | awk '{print "rtc_events="$3}'; }
state() { echo "ss=$(ss) bl=$(cat /sys/class/backlight/*/bl_power) idle=$(loginctl show-session c7 -p IdleHint --value) locked=$(loginctl show-session c7 -p LockedHint --value) $(rtcws) fpirq=$(cat /sys/devices/platform/focaltech_fp/focaltech_fp/focaltech_fp/irq_count) bat=$(cat $B/capacity)% $(cat $B/status)"; }
# trace
echo 0 > /sys/kernel/tracing/tracing_on; echo > /sys/kernel/tracing/trace
for e in rtc/rtc_set_alarm rtc/rtc_timer_enqueue rtc/rtc_timer_fired alarmtimer/alarmtimer_suspend alarmtimer/alarmtimer_fired alarmtimer/alarmtimer_start alarmtimer/alarmtimer_cancel power/wakeup_source_activate; do echo 1 > /sys/kernel/tracing/events/$e/enable; done
echo 1 > /sys/kernel/tracing/options/stacktrace; echo 1 > /sys/kernel/tracing/tracing_on
( timeout $((SLP + WATCH + 120)) cat /sys/kernel/tracing/trace_pipe > $D/trace-$TAG.txt ) &
# idle monitor signals
( timeout $((SLP + WATCH + 120)) su user -c "DBUS_SESSION_BUS_ADDRESS=$BUS busctl --user monitor --match 'type=signal,interface=org.gnome.Mutter.IdleMonitor' --match 'type=signal,interface=org.gnome.ScreenSaver' --match 'type=method_call,interface=org.gnome.ScreenSaver'" >> $D/idle-$TAG.txt 2>&1 ) &
log "start tag=$TAG sleep=${SLP}s watch=${WATCH}s; $(state)"
log "inhibitors: $(systemd-inhibit --list --no-legend 2>/dev/null | awk '$7!="" {print $1"/"$6"/"$NF}' | tr '\n' ' ')"
sleep 5
rtcwake -m no -s $SLP >> $L 2>&1
t0=$(date +%s); n0=$(ss)
systemctl suspend -i
sleep 3
log "after suspend call: $(state)"
last=$(date +%s); woke=0; resus=0
while [ $(( $(date +%s) - t0 )) -lt $((SLP + WATCH)) ]; do
	sleep 10
	now=$(date +%s)
	if [ $((now - last)) -gt 30 ]; then log "TIME GAP $((now - last))s (asleep)"; fi
	last=$now
	s=$(ss)
	[ $s -gt $n0 ] && [ $woke = 0 ] && { woke=$now; log "woke; suspends=$((s - n0))"; }
	[ $woke != 0 ] && [ $s -gt $((n0 + 1)) ] && { resus=1; log "RE-SUSPENDED on its own (suspends=$((s - n0)))"; }
	log "$(state)"
done
log "end: re-suspended=$resus; wakealarm=$(cat /sys/class/rtc/rtc0/wakealarm)"
echo 0 > /sys/class/rtc/rtc0/wakealarm
echo 0 > /sys/kernel/tracing/tracing_on; echo 0 > /sys/kernel/tracing/options/stacktrace
for e in rtc alarmtimer; do echo 0 > /sys/kernel/tracing/events/$e/enable; done; echo 0 > /sys/kernel/tracing/events/power/wakeup_source_activate
log "wakealarm after cleanup: $(cat /sys/class/rtc/rtc0/wakealarm)"
log "watcher: $(su user -c 'XDG_RUNTIME_DIR=/run/user/10000 systemctl --user is-active fp5-qtee-unlock') session=$(pidof fp5-qtee-session)"
