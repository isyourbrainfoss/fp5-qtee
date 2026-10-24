#!/bin/sh
# Remove fp5-upgrade-guard. Keeps /var/lib/fp5-upgrade-guard (the saved copies)
# unless --purge is given. Does not touch any of the guarded files.
set -eu
S() { sudo -n "$@"; }
S systemctl disable fp5-upgrade-guard.service 2>/dev/null || true
S rm -f /etc/systemd/system/fp5-upgrade-guard.service /etc/apk/commit_hooks.d/zz-fp5-upgrade-guard.sh /usr/local/sbin/fp5-upgrade-guard
S systemctl daemon-reload
[ "${1:-}" = --purge ] && S rm -rf /var/lib/fp5-upgrade-guard
echo removed
