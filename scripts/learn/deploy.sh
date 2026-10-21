#!/bin/sh
# Deploy the template-learning session (C) and warm mode on the phone.
# Needs a backup of /mnt/persist/misc that matches the store right now.
# Does not reboot, flash or enroll.
set -eu
D=$(cd "$(dirname "$0")" && pwd)
. "$D/common.sh"
C_SHA=$(cat "$D/C.sha256")

last=$(ls -1d "$KEEP"/backup/*/ 2>/dev/null | sort | tail -n 1)
[ -n "$last" ] && [ -f "$last/MANIFEST.sha256" ] || die "no storage backup; run fp5-qtee backup"
[ "$(store_manifest)" = "$(cat "$last/MANIFEST.sha256")" ] ||
	die "storage changed since backup $last; take a new backup first"
say "storage backup $last matches $STORE"
# The backup copy is owned by user; restore.sh puts these owners back.
[ -f "$last/OWNERS.txt" ] || store_owners > "$last/OWNERS.txt"

take_lock
# The watcher config before this deploy, for restore.sh.
if [ ! -d "$D/watcher-config.before" ]; then
	mkdir -p "$D/watcher-config.before"
	cp -a "$DROP/." "$D/watcher-config.before/" 2>/dev/null || true
fi
[ -f "$D/fp5-qtee-session.prev" ] || cp "$LIVE" "$D/fp5-qtee-session.prev"
stop_all
install_bin "$D/fp5-qtee-session.C" "$C_SHA"
mkdir -p "$DROP"
printf '[Service]\nEnvironment=FP5_QTEE_WARM=1\n' > "$DROP/warm.conf"
say "warm mode drop-in $DROP/warm.conf"
flock -u 9
start_unit
echo "$(date -u +%FT%TZ) deploy C $C_SHA warm=1" >> "$D/history.txt"
