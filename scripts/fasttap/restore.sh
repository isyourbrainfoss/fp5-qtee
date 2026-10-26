#!/bin/sh
# Undo deploy.sh: put back the session binary and watcher config from
# $K/before, then restart the watcher. Storage is not touched (learning
# saves stay; see scripts/learn/restore.sh --storage for that, with its
# RPMB anti-rollback caveat).
set -eu
D=$(cd "$(dirname "$0")" && pwd)
. "$D/common.sh"
[ -f "$BEFORE/fp5-qtee-session" ] || die "no backup in $BEFORE"
want=$(cat "$BEFORE/fp5-qtee-session.sha256")
[ "$(sha "$BEFORE/fp5-qtee-session")" = "$want" ] || die "backup binary damaged"
take_lock
stop_all
install_file "$BEFORE/fp5-qtee-session" "$LIVE"
rm -f "$OURS"
for f in "$BEFORE/dropins"/*; do
	[ -f "$f" ] && cp -a "$f" "$DROP/"
done
say "restored binary $want; drop-ins now: $(ls "$DROP" | tr '\n' ' ')"
flock -u 9
start_unit
check_null
echo "$(date +%FT%T%z) restore $want" >> "$K/history.txt"
