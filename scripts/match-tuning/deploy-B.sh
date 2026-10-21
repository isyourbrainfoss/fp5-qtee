#!/bin/sh
# Put the match-tuning session binary (B) in place of the live one (A).
# Run on the phone as user "user". Not run by the agent that wrote it.
# Does not reboot, flash, enroll, delete a template, or touch /mnt/persist.
# The watcher picks the new binary up at its next listen; restart it so
# the very next press already uses B.
set -eu
D=$(cd "$(dirname "$0")" && pwd)
LIVE=/home/user/fp5-qtee-keep/session/fp5-qtee-session
A_SHA=fb4ee59b867ce41635f1dac9a5ccc0ac3c39b7ab96e007f2bd71f1d27db85b83
B_SHA=09b9f0f14e8fe0ddee6f760bbe0c833775cbd8f525501d77e8c9cc34a0596670

have() { sha256sum "$1" | cut -d' ' -f1; }
[ "$(have "$D/fp5-qtee-session.B")" = "$B_SHA" ] || { echo "B binary hash mismatch"; exit 1; }
[ "$(have "$D/fp5-qtee-session.A")" = "$A_SHA" ] || { echo "A backup hash mismatch"; exit 1; }
cur=$(have "$LIVE")
if [ "$cur" = "$B_SHA" ]; then echo "B is already live"; exit 0; fi
if [ "$cur" != "$A_SHA" ] && [ "${FORCE:-0}" != 1 ]; then
	echo "live binary is $cur, not A ($A_SHA). Saving it and stopping."
	cp -p "$LIVE" "$D/fp5-qtee-session.live-$(date -u +%Y%m%dT%H%M%SZ)"
	echo "Re-run with FORCE=1 to replace it anyway."
	exit 1
fi
# Do not swap while the finger-list tooling holds the phone lock.
exec 9>/tmp/fp5-qtee-phone.lock
flock -w 120 9 || { echo "phone lock busy"; exit 1; }
cp "$D/fp5-qtee-session.B" "$LIVE.new"
chmod 0755 "$LIVE.new"
mv -f "$LIVE.new" "$LIVE"   # rename: a running session keeps its old inode
echo "B installed: $(have "$LIVE")"
echo "$(date -u +%FT%TZ) deploy B $B_SHA" >> "$D/history.txt"
systemctl --user restart fp5-qtee-unlock.service && echo "watcher restarted"
