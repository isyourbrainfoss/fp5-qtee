#!/bin/sh
# Short real suspend (logind path, PrepareForSleep, delay locks) that an
# RTC alarm ends after $1 s (default 30). Run detached; SSH drops while
# suspended. Blanks the screen first (Phosh SetActive true; the phone
# should already be locked). Log: $KEEP/logs/fp-wake-test-<UTC>.txt
set -u
D=$(cd "$(dirname "$0")" && pwd)
. "$D/common.sh"
secs=${1:-30}
log=$KEEP/logs/fp-wake-test-$(date -u +%Y%m%dT%H%M%SZ).txt
{
	say "before: $(cat /sys/class/focaltech_fp/focaltech_fp/irq_count) irq, wake_count=$(cat /sys/class/focaltech_fp/focaltech_fp/wake_count), wakeup=$(cat $PDEV/power/wakeup)"
	gdbus call --session --dest org.gnome.ScreenSaver --object-path /org/gnome/ScreenSaver \
		--method org.gnome.ScreenSaver.SetActive true >/dev/null 2>&1
	sleep 4
	say "panel=$(cat /sys/class/drm/*DSI*/dpms)"
	sudo -n rtcwake -m no -s "$secs"
	sudo -n systemctl suspend -i
	sleep $((secs + 20))
	say "after: irq_count=$(cat /sys/class/focaltech_fp/focaltech_fp/irq_count) wake_count=$(cat /sys/class/focaltech_fp/focaltech_fp/wake_count)"
	sudo -n dmesg | grep -E "focaltech_fp.*(suspend|resume)|PM: suspend|Wakeup source|wakeup" | tail -12
	tail -6 "$KEEP/logs/unlock-watcher.log"
	journalctl --no-pager --since "-3min" | grep -E "PM: suspend|suspend requested|system will suspend" | tail -5
} > "$log" 2>&1
