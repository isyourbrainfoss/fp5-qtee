#!/bin/sh
# Undo deploy.sh: previous fp5_fprintd.py and Fingerprints app back, honor
# drop-in removed (unless it existed before), watcher script back if
# deploy.sh --watcher replaced it. No storage, PAM or sensor changes.
set -u
K=$(cd "$(dirname "$0")" && pwd)
ORIG=$K/orig
UNIT=fp5-fprintd.service
HONOR_DROPIN=/etc/systemd/system/$UNIT.d/zz-lockout.conf
S() { sudo -n "$@"; }
[ -f "$ORIG/saved" ] || { echo "nothing saved in $ORIG; was deploy.sh run?"; exit 1; }
if [ ! -f "$ORIG/had-honor-dropin" ]; then
	S rm -f "$HONOR_DROPIN"
	S systemctl daemon-reload
fi
S install -m 755 "$ORIG/fp5_fprintd.py" /usr/local/lib/fp5-qtee/fp5_fprintd.py
if [ -f "$ORIG/fp5-fingerprints" ]; then
	install -m 755 "$ORIG/fp5-fingerprints" "$HOME/.local/bin/fp5-fingerprints"
fi
S systemctl restart $UNIT
sleep 1; systemctl is-active $UNIT
if [ -f "$ORIG/watcher-path" ]; then
	W=$(cat "$ORIG/watcher-path")
	cp "$ORIG/fp5_qtee_unlock.py" "$W.new" && mv -f "$W.new" "$W"
	systemctl --user restart fp5-qtee-unlock.service
	sleep 2; systemctl --user is-active fp5-qtee-unlock.service
fi
echo "$(date -u +%FT%TZ) restore" >> "$K/history.txt"
echo "restored."
