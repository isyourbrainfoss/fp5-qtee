#!/bin/sh
# Deploy fingerprint lockout on D-Bus (branch fprint-lockout). Run on the
# phone, screen UNLOCKED, from the kit dir as the user (uses sudo -n).
# No reboot, no fingerprint storage, no PAM files, no sensor access.
#
#   deploy.sh              daemon (org.fp5.Fprint.Manage lockout props +
#                          LockoutChanged) and the Fingerprints app banner
#   deploy.sh --honor      also: VerifyStart during a lockout answers
#                          verify-disconnected (sudo/polkit fall back to the
#                          password). Drop-in zz-lockout.conf.
#   deploy.sh --watcher    also: the watcher with transient lockout notices
#                          (replaces the fp5_qtee_unlock.py its ExecStart runs,
#                          restarts fp5-qtee-unlock.service)
# Flags can be combined. Undo with ./restore.sh (all of it).
#
# Kit = this dir + fprintd/fp5_fprintd.py, fprintd/ui/fp5_fingerprints.py and
# (for --watcher) app/fp5_qtee_unlock.py copied flat next to this script.
set -eu
K=$(cd "$(dirname "$0")" && pwd)
ORIG=$K/orig
LIB=/usr/local/lib/fp5-qtee
UNIT=fp5-fprintd.service
HONOR_DROPIN=/etc/systemd/system/$UNIT.d/zz-lockout.conf
S() { sudo -n "$@"; }
honor=0; watcher=0
for a in "$@"; do
	case $a in
	--honor) honor=1 ;;
	--watcher) watcher=1 ;;
	*) echo "unknown flag $a"; exit 1 ;;
	esac
done
need="fp5_fprintd.py fp5_fingerprints.py"
[ "$watcher" = 1 ] && need="$need fp5_qtee_unlock.py"
for f in $need; do [ -f "$K/$f" ] || { echo "missing $K/$f"; exit 1; }; done
python3 -m py_compile "$K/fp5_fprintd.py" "$K/fp5_fingerprints.py"
[ "$watcher" = 1 ] && python3 -m py_compile "$K/fp5_qtee_unlock.py"
if [ "$(loginctl show-session "$(loginctl list-sessions --no-legend | awk '$4 ~ /seat/ {print $1; exit}')" -p LockedHint --value 2>/dev/null)" = yes ]; then
	echo "screen is locked; unlock first"; exit 1
fi

# Where the running watcher's script lives (fp-wake drop-in: <kit>/app).
WATCHER_PY=$(systemctl --user cat fp5-qtee-unlock.service 2>/dev/null |
	sed -n 's|^ExecStart=.*python3[^ ]* \([^ ]*fp5_qtee_unlock\.py\).*|\1|p' | tail -n 1)

# 1. Save what we replace (once).
mkdir -p "$ORIG"
if [ ! -f "$ORIG/saved" ]; then
	S cp -a $LIB/fp5_fprintd.py "$ORIG/fp5_fprintd.py"
	[ -f "$HOME/.local/bin/fp5-fingerprints" ] && cp -a "$HOME/.local/bin/fp5-fingerprints" "$ORIG/fp5-fingerprints"
	S test -e "$HONOR_DROPIN" && echo yes > "$ORIG/had-honor-dropin" || true
	S chown -R "$(id -u):$(id -g)" "$ORIG"
	date -u +%FT%TZ > "$ORIG/saved"
fi
if [ "$watcher" = 1 ]; then
	[ -n "$WATCHER_PY" ] && [ -f "$WATCHER_PY" ] || { echo "cannot find the watcher's fp5_qtee_unlock.py (got '$WATCHER_PY')"; exit 1; }
	if [ ! -f "$ORIG/watcher-path" ]; then
		cp -a "$WATCHER_PY" "$ORIG/fp5_qtee_unlock.py"
		echo "$WATCHER_PY" > "$ORIG/watcher-path"
	fi
fi

# 2. Sandbox check: the daemon (root) reads /run/user/<uid>/fp5-qtee-lockout.
ph=$(systemctl show $UNIT -p ProtectHome --value 2>/dev/null || true)
case $ph in yes|read-only|tmpfs) echo "WARNING: $UNIT has ProtectHome=$ph; /run/user may be hidden and lockout will read as 'none'";; esac

# 3. Install daemon (+ optional honor drop-in) and app.
S install -m 755 "$K/fp5_fprintd.py" $LIB/fp5_fprintd.py
if [ "$honor" = 1 ]; then
	S install -d -m 755 /etc/systemd/system/$UNIT.d
	printf '[Service]\nEnvironment=FP5_FPRINTD_HONOR_LOCKOUT=1\n' | S tee "$HONOR_DROPIN" >/dev/null
	S systemctl daemon-reload
fi
mkdir -p "$HOME/.local/bin"
install -m 755 "$K/fp5_fingerprints.py" "$HOME/.local/bin/fp5-fingerprints"
S systemctl restart $UNIT
sleep 2
systemctl is-active $UNIT
busctl --system get-property net.reactivated.Fprint /net/reactivated/Fprint/Device/0 \
	org.fp5.Fprint.Manage locked-out lockout-remaining lockout-reason failed-attempts
journalctl -u $UNIT -n 5 --no-pager -o cat 2>/dev/null | grep -i lockout || true

# 4. Optional watcher.
if [ "$watcher" = 1 ]; then
	cp "$K/fp5_qtee_unlock.py" "$WATCHER_PY.new"
	mv -f "$WATCHER_PY.new" "$WATCHER_PY"
	systemctl --user restart fp5-qtee-unlock.service
	sleep 2
	systemctl --user is-active fp5-qtee-unlock.service
fi
echo "$(date -u +%FT%TZ) deploy honor=$honor watcher=$watcher" >> "$K/history.txt"
echo "deployed. restore: $K/restore.sh"
