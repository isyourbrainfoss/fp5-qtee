#!/bin/sh
# Put the pre-match-tuning session binary (A, fb4ee59b = main 076720f) back.
# Does not reboot, flash, enroll, delete a template, or touch /mnt/persist.
set -eu
D=$(cd "$(dirname "$0")" && pwd)
LIVE=/home/user/fp5-qtee-keep/session/fp5-qtee-session
A_SHA=fb4ee59b867ce41635f1dac9a5ccc0ac3c39b7ab96e007f2bd71f1d27db85b83
[ "$(sha256sum "$D/fp5-qtee-session.A" | cut -d' ' -f1)" = "$A_SHA" ] || { echo "A backup hash mismatch"; exit 1; }
exec 9>/tmp/fp5-qtee-phone.lock
flock -w 120 9 || { echo "phone lock busy"; exit 1; }
cp "$D/fp5-qtee-session.A" "$LIVE.new"
chmod 0755 "$LIVE.new"
mv -f "$LIVE.new" "$LIVE"
echo "A restored: $(sha256sum "$LIVE" | cut -d' ' -f1)"
echo "$(date -u +%FT%TZ) restore A $A_SHA" >> "$D/history.txt"
systemctl --user restart fp5-qtee-unlock.service && echo "watcher restarted"
