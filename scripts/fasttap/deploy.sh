#!/bin/sh
# Deploy fast-tap-20261026: session binary + watcher app dir + drop-in.
# Run on the phone from $K. No reboot, flash, kernel install or enroll.
# Needs a storage backup that matches the store now (learning writes it).
set -eu
D=$(cd "$(dirname "$0")" && pwd)
. "$D/common.sh"
NEW_BIN=$K/build/fp5-qtee-session
[ -f "$NEW_BIN" ] || die "build $NEW_BIN first"
[ -f "$K/app/fp5_qtee_unlock.py" ] || die "no $K/app"

last=$(ls -1d "$KEEP"/backup/*/ 2>/dev/null | sort | tail -n 1)
[ -n "$last" ] && [ -f "$last/MANIFEST.sha256" ] || die "no storage backup; run fp5-qtee backup"
[ "$(store_manifest)" = "$(cat "$last/MANIFEST.sha256")" ] ||
	die "storage changed since backup $last; take a new backup first"
say "storage backup $last matches $STORE"

take_lock
# Backups of everything this deploy changes. Never overwritten.
if [ ! -d "$BEFORE" ]; then
	mkdir -p "$BEFORE/dropins"
	cp "$LIVE" "$BEFORE/fp5-qtee-session"
	sha "$LIVE" > "$BEFORE/fp5-qtee-session.sha256"
	cp -a "$DROP/." "$BEFORE/dropins/" 2>/dev/null || true
	systemctl --user cat "$UNIT" > "$BEFORE/unit-cat.txt" 2>&1 || true
	say "backed up live binary $(cat "$BEFORE/fp5-qtee-session.sha256") and drop-ins: $(ls "$BEFORE/dropins" | tr '\n' ' ')"
fi
stop_all
install_file "$NEW_BIN" "$LIVE"
say "live session binary $(sha "$LIVE")"
mkdir -p "$DROP"
cat > "$OURS.tmp" <<EOT
# fast-tap-20261026. restore.sh removes this file.
[Service]
ExecStart=
ExecStart=/usr/sbin/python3 $K/app/fp5_qtee_unlock.py
Environment=PYTHONPATH=$K/app
EOT
mv -f "$OURS.tmp" "$OURS"
say "drop-in $OURS"
flock -u 9
systemctl --user daemon-reload
systemctl --user show -p ExecStart "$UNIT" | grep -q "$K/app/fp5_qtee_unlock.py" ||
	die "ExecStart is not ours; another drop-in sorts after $OURS (run restore.sh)"
start_unit
check_null
echo "$(date +%FT%T%z) deploy $(sha "$LIVE")" >> "$K/history.txt"
