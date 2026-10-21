#!/bin/sh
# Undo deploy.sh: session binary fb4ee59b (main 076720f) and the watcher
# drop-ins from before the deploy (parity.conf only, no warm.conf).
#
#   restore.sh                     binary + watcher config
#   restore.sh --storage <backup>  also put /mnt/persist/misc back from
#                                  <backup>/misc (e.g. $KEEP/backup/20261021T202431Z)
#
# Storage restore caveat: the TEE keeps an anti-rollback counter in RPMB.
# If learning saves moved it, the old files may be refused on load (the
# finger list comes back empty) and the fingers have to be enrolled again.
set -eu
D=$(cd "$(dirname "$0")" && pwd)
. "$D/common.sh"
bk=""
if [ "${1:-}" = "--storage" ]; then
	bk=${2:-}
	[ -n "$bk" ] && [ -d "$bk/misc" ] && [ -f "$bk/MANIFEST.sha256" ] ||
		die "usage: restore.sh --storage <backup dir with misc/ and MANIFEST.sha256>"
	(cd "$bk/misc" && sha256sum -c -s ../MANIFEST.sha256) || die "backup $bk is damaged"
	[ -f "$bk/OWNERS.txt" ] || die "backup $bk has no OWNERS.txt (uid:gid mode path)"
fi
take_lock
stop_all
install_bin "$D/fp5-qtee-session.A" "$A_SHA"
rm -f "$DROP/warm.conf"
if [ -d "$D/watcher-config.before" ]; then
	for f in "$D/watcher-config.before"/*; do
		[ -e "$f" ] && cp -a "$f" "$DROP/"
	done
fi
say "watcher drop-ins now: $(ls "$DROP" | tr '\n' ' ')"
if [ -n "$bk" ]; then
	now="$KEEP/backup/$(date -u +%Y%m%dT%H%M%SZ)-before-restore"
	mkdir -p "$now"
	sudo -n cp -a "$STORE" "$now/misc"
	sudo -n chown -R "$(id -u):$(id -g)" "$now/misc"
	store_manifest > "$now/MANIFEST.sha256"
	store_owners > "$now/OWNERS.txt"
	say "current storage saved to $now"
	sudo -n find "$STORE" -mindepth 1 -delete
	sudo -n cp -a "$bk/misc/." "$STORE/"
	while read -r ug mode path; do
		case $path in "$STORE"|"$STORE"/*) ;; *) continue ;; esac
		[ -e "$path" ] || continue
		sudo -n chown "$ug" "$path"
		sudo -n chmod "$mode" "$path"
	done < "$bk/OWNERS.txt"
	store_owners | diff -q - "$bk/OWNERS.txt" >/dev/null ||
		say "WARNING: owners/modes differ from $bk/OWNERS.txt (check: diff)"
	[ "$(store_manifest)" = "$(cat "$bk/MANIFEST.sha256")" ] ||
		die "store does not match $bk after copy (saved copy in $now)"
	sudo -n sync
	say "storage restored from $bk"
fi
flock -u 9
start_unit
echo "$(date -u +%FT%TZ) restore A $A_SHA storage=${bk:-no}" >> "$D/history.txt"
