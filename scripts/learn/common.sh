# Shared by deploy.sh and restore.sh. Phone side, user "user".
KEEP=/home/user/fp5-qtee-keep
LIVE=$KEEP/session/fp5-qtee-session
STORE=/mnt/persist/misc
LOCK=/tmp/fp5-qtee-phone.lock
UNIT=fp5-qtee-unlock.service
DROP=$HOME/.config/systemd/user/$UNIT.d
A_SHA=fb4ee59b867ce41635f1dac9a5ccc0ac3c39b7ab96e007f2bd71f1d27db85b83
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] || \
	export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"

say() { echo "$(date -u +%FT%TZ) $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }

store_manifest() {
	sudo -n sh -c "cd '$STORE' && find . -type f -print0 | sort -z | xargs -0 -r sha256sum"
}

take_lock() {
	exec 9>"$LOCK"
	flock -w 120 9 || die "phone lock $LOCK busy"
}

# Stop the watcher and wait until no session process is left. The
# watcher's session runs under sudo in its own scope, so stopping the
# unit does not end it: after 5 s it gets the SIGTERM its own
# `timeout 1800` would send (as app/fp5-qtee does), after 20 s SIGKILL.
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

install_bin() { # $1 source, $2 expected sha
	[ "$(sha "$1")" = "$2" ] || die "$1 is not $2"
	cp "$1" "$LIVE.new"
	chmod 0755 "$LIVE.new"
	mv -f "$LIVE.new" "$LIVE"
	[ "$(sha "$LIVE")" = "$2" ] || die "live binary is not $2 after install"
	say "live session binary $2"
}

start_unit() {
	systemctl --user daemon-reload
	systemctl --user start "$UNIT"
	sleep 2
	systemctl --user is-active --quiet "$UNIT" || die "$UNIT did not start"
	say "$UNIT active"
}

# uid:gid mode path, numeric, for every entry under the store.
store_owners() {
	sudo -n find "$STORE" -exec stat -c '%u:%g %a %n' {} + | sort -k3
}
