#!/bin/sh
# gsd-power has RefuseManualStart/Stop, so make systemd restart it the way it
# would after a crash. (gnome-session-ctl --exec-stop-check only acts on
# start-limit-hit, so one restart is harmless.)
pid=$(pgrep -x gsd-power) || { echo "gsd-power not running"; exit 1; }
kill -KILL "$pid"
for i in 1 2 3 4 5 6 7 8 9 10; do
	sleep 0.5
	n=$(pgrep -x gsd-power) && [ "$n" != "$pid" ] && { echo "gsd-power restarted: pid $n"; exit 0; }
done
echo "gsd-power did not come back" >&2; exit 1
