#!/bin/sh
# apk commit hook (/etc/apk/commit_hooks.d/). apk runs it as root with
# "pre-commit" or "post-commit". After every apk transaction this re-applies
# what fp5-upgrade-guard can put back and logs the result. It never fails the
# transaction (always exit 0).
[ "${1:-}" = post-commit ] || exit 0
LOG=/var/log/fp5-upgrade-guard.log
{
	echo "=== $(date -u +%FT%TZ) apk post-commit"
	/usr/local/sbin/fp5-upgrade-guard fix
	echo "exit $?"
} >> "$LOG" 2>&1
tail -n 60 "$LOG" | awk '/^=== /{buf=""} {buf=buf $0 "\n"} END{printf "%s", buf}' > "$LOG.last"
if grep -q '^FAIL' "$LOG.last"; then
	echo "fp5-upgrade-guard: problems after this upgrade, see $LOG.last" >&2
	grep '^FAIL' "$LOG.last" >&2
fi
exit 0
