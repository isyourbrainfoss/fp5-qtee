#!/bin/sh
# Install fp5-upgrade-guard on the phone (as user, sudo -n). No reboot.
#   sh install.sh             install the tool, apk hook and boot check
#   sh install.sh --snapshot  also record the current state as known-good
# Take the snapshot only while the fixes are live and working (patched
# gsd-power, cell-backoff, fprintd...), or the guard will "restore" a bad state.
# Undo: sh uninstall.sh
set -eu
D=$(cd "$(dirname "$0")" && pwd); S() { sudo -n "$@"; }
sh -n "$D/fp5-upgrade-guard"; sh -n "$D/zz-fp5-upgrade-guard.sh"
S install -D -m 755 "$D/fp5-upgrade-guard" /usr/local/sbin/fp5-upgrade-guard
S install -D -m 755 "$D/zz-fp5-upgrade-guard.sh" /etc/apk/commit_hooks.d/zz-fp5-upgrade-guard.sh
S install -m 644 "$D/fp5-upgrade-guard.service" /etc/systemd/system/fp5-upgrade-guard.service
S systemctl daemon-reload
S systemctl enable fp5-upgrade-guard.service
[ "${1:-}" = --snapshot ] && S /usr/local/sbin/fp5-upgrade-guard snapshot
S /usr/local/sbin/fp5-upgrade-guard check || true
