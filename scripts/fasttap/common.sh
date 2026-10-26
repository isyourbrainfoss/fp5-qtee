# fast-tap kit, phone side, user "user". Sourced by deploy.sh/restore.sh.
KEEP=/home/user/fp5-qtee-keep
K=$KEEP/fasttap-20261026
LIVE=$KEEP/session/fp5-qtee-session
STORE=/mnt/persist/misc
LOCK=/tmp/fp5-qtee-phone.lock
UNIT=fp5-qtee-unlock.service
DROP=$HOME/.config/systemd/user/$UNIT.d
# Must sort after every other drop-in (zzz-fp-fix-20261025.conf sets
# ExecStart too; the last one in name order wins).
OURS=$DROP/zzzz-fasttap.conf
BEFORE=$K/before
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] || \
	export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"

say() { echo "$(date +%FT%T%z) $*" | tee -a "$K/PROGRESS.md"; }
die() { echo "ERROR: $*" >&2; echo "$(date +%FT%T%z) ERROR: $*" >> "$K/PROGRESS.md"; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }

store_manifest() {
	sudo -n sh -c "cd '$STORE' && find . -type f -print0 | sort -z | xargs -0 -r sha256sum"
}

take_lock() {
	exec 9>"$LOCK"
	flock -w 120 9 || die "phone lock $LOCK busy"
}

# Same as scripts/learn: stop the watcher, then wait for its session.
stop_all() {
	systemctl --user stop "$UNIT" || true
	i=0
	while pids=$(pidof fp5-qtee-session 2>/dev/null); do
		i=$((i + 1))
		if [ "$i" = 10 ]; then
			say "ending session $pids"
			# shellcheck disable=SC2086
			sudo -n kill -TERM $pids 2>/dev/null || true
		fi
		if [ "$i" = 40 ]; then
			# shellcheck disable=SC2086
			sudo -n kill -KILL $pids 2>/dev/null || true
		fi
		[ "$i" -le 60 ] || die "a session is still running after 30 s"
		sleep 0.5
	done
}

# Regular files only, never a device node; copy to .new then rename.
install_file() { # $1 source, $2 destination
	[ -f "$1" ] || die "$1 is not a regular file"
	if [ -e "$2" ] && [ ! -f "$2" ]; then
		die "$2 exists and is not a regular file"
	fi
	cp "$1" "$2.new"
	chmod "$(stat -c %a "$1")" "$2.new" # BusyBox: no --reference
	mv -f "$2.new" "$2"
	[ "$(sha "$1")" = "$(sha "$2")" ] || die "$2 differs from $1 after install"
}

start_unit() {
	systemctl --user daemon-reload
	systemctl --user start "$UNIT"
	sleep 3
	systemctl --user is-active --quiet "$UNIT" || die "$UNIT did not start"
	say "$UNIT active"
}

check_null() {
	[ -c /dev/null ] || die "/dev/null is not a character device"
	say "/dev/null is a character device: $(stat -c '%F %t:%T' /dev/null)"
}
