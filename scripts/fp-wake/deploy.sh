#!/bin/sh
# Touch-to-wake: focaltech_fp_life 0.8-wake (IRQ wake on gpio34/PDC 77)
# and the watcher with FP5_QTEE_WAKE=1 (dark arming + sleep delay lock +
# screen on after a dark hit). No reboot, no flashing, no storage writes.
#   deploy.sh            module + watcher
#   deploy.sh --no-install   load the module but leave /lib/modules alone
set -eu
D=$(cd "$(dirname "$0")" && pwd)
. "$D/common.sh"
install=1
[ "${1:-}" = "--no-install" ] && install=0
NEW_SHA=$(cat "$D/ko.sha256")
[ "$(sha "$D/$MOD.ko")" = "$NEW_SHA" ] || die "$D/$MOD.ko is not $NEW_SHA"
[ -f "$D/$MOD.ko.orig" ] || die "no $D/$MOD.ko.orig"
[ "$(sha "$D/$MOD.ko.orig")" = "$ORIG_SHA" ] || die "$MOD.ko.orig is not $ORIG_SHA"

take_lock
stop_all
swap_module "$D/$MOD.ko"
if [ "$install" = 1 ]; then
	sudo -n cp "$D/$MOD.ko" "$SYS_KO.new"
	sudo -n mv -f "$SYS_KO.new" "$SYS_KO"
	[ "$(sha "$SYS_KO")" = "$NEW_SHA" ] || die "$SYS_KO is not $NEW_SHA"
	say "installed $SYS_KO (survives reboot; restore.sh puts $ORIG_SHA back)"
fi
w=$(cat "$PDEV/power/wakeup" 2>/dev/null || echo missing)
say "power/wakeup=$w"
[ "$w" = enabled ] || die "$PDEV/power/wakeup is '$w', not enabled"
mkdir -p "$DROP"
cat > "$WAKE_CONF" <<CONF
[Service]
ExecStart=
ExecStart=/usr/sbin/python3 $D/app/fp5_qtee_unlock.py
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$D/app
Environment=FP5_QTEE_WARM=1
Environment=FP5_QTEE_WAKE=1
CONF
say "watcher drop-in $WAKE_CONF"
flock -u 9
start_unit
echo "$(date -u +%FT%TZ) deploy ko=$NEW_SHA install=$install" >> "$D/history.txt"
