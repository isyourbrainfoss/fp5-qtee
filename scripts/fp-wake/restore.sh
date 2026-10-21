#!/bin/sh
# Undo deploy.sh: original focaltech_fp_life 0.7 (e9579dca) loaded and in
# /lib/modules, watcher drop-in removed (parity.conf + warm.conf remain,
# i.e. the template-learning deploy of 2026-10-21). No reboot needed.
set -eu
D=$(cd "$(dirname "$0")" && pwd)
. "$D/common.sh"
[ "$(sha "$D/$MOD.ko.orig")" = "$ORIG_SHA" ] || die "$MOD.ko.orig is not $ORIG_SHA"
take_lock
stop_all
rm -f "$WAKE_CONF"
if [ "$(sha "$SYS_KO")" != "$ORIG_SHA" ]; then
	sudo -n cp "$D/$MOD.ko.orig" "$SYS_KO.new"
	sudo -n mv -f "$SYS_KO.new" "$SYS_KO"
fi
[ "$(sha "$SYS_KO")" = "$ORIG_SHA" ] || die "$SYS_KO is not $ORIG_SHA"
swap_module "$SYS_KO"
say "drop-ins now: $(ls "$DROP" | tr '\n' ' ')"
flock -u 9
start_unit
echo "$(date -u +%FT%TZ) restore ko=$ORIG_SHA" >> "$D/history.txt"
