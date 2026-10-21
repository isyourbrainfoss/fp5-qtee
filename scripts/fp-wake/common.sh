# Shared by deploy.sh and restore.sh (phone, user "user").
KEEP=/home/user/fp5-qtee-keep
STORE=/mnt/persist/misc
LOCK=/tmp/fp5-qtee-phone.lock
UNIT=fp5-qtee-unlock.service
DROP=$HOME/.config/systemd/user/$UNIT.d
WAKE_CONF=$DROP/zz-fp-wake.conf
MOD=focaltech_fp_life
SYS_KO=/lib/modules/$(uname -r)/extra/$MOD.ko
PDEV=/sys/devices/platform/focaltech_fp
ORIG_SHA=e9579dcaf224f5d87475003faf2083ba48069cb357531ce85baae8da9c9e1f27
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] || \
	export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"

say() { echo "$(date -u +%FT%TZ) $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }

take_lock() {
	exec 9>"$LOCK"
	flock -w 120 9 || die "phone lock $LOCK busy"
}

# The session runs under sudo in its own scope: stopping the unit does
# not end it. TERM after 5 s (what its timeout would send), KILL at 20 s.
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

# Load $1 as focaltech_fp_life (replacing whatever is loaded).
swap_module() {
	ko=$1
	if [ -d /sys/module/$MOD ]; then
		sudo -n rmmod $MOD || die "rmmod $MOD failed"
	fi
	sudo -n insmod "$ko" auto_spiclk=1 || die "insmod $ko failed"
	[ -d /sys/class/focaltech_fp/focaltech_fp ] || die "$MOD loaded but no sysfs"
	say "loaded $MOD $(cat /sys/module/$MOD/version 2>/dev/null) from $ko"
}

start_unit() {
	systemctl --user daemon-reload
	systemctl --user start "$UNIT"
	sleep 2
	systemctl --user is-active --quiet "$UNIT" || die "$UNIT did not start"
	say "$UNIT active"
}
