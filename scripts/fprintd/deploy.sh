#!/bin/sh
# Deploy the QTEE-backed fprintd on the phone. Run from the kit dir as user
# (uses sudo -n). Reversible with ./restore.sh. Does not touch the unlock
# watcher, /etc/pam.d/sudo, /etc/pam.d/phosh or /etc/pam.d/polkit-1.
set -eu
K=$(cd "$(dirname "$0")" && pwd)
ORIG=$K/orig
UNIT=fp5-fprintd.service
DROPIN=/etc/systemd/system/$UNIT.d/zz-qtee.conf
LIB=/usr/local/lib/fp5-qtee
POLICY=/usr/share/polkit-1/actions/net.reactivated.fprint.device.policy
S() { sudo -n "$@"; }

[ -f "$K/fp5_fprintd.py" ] || { echo "missing fp5_fprintd.py"; exit 1; }
# 1. Fingerprint storage backup with manifest (the daemon also backs up
#    before every enroll/delete).
B=$HOME/fp5-qtee-keep/backup/$(date -u +%Y%m%dT%H%M%SZ)-fprintd
mkdir -p "$B"
S cp -a /mnt/persist/misc "$B/misc"
S chown -R "$(id -u):$(id -g)" "$B"
(cd "$B/misc" && find . -type f | sort | xargs sha256sum) > "$B/MANIFEST.sha256"
(S sh -c "cd /mnt/persist/misc && find . -type f | sort | xargs sha256sum") | cmp -s - "$B/MANIFEST.sha256" \
	|| { echo "backup does not match store"; exit 1; }
echo "$B" > "$K/STORAGE-BACKUP"
echo "storage backup: $B"

# 2. Save what we replace (only once).
if [ ! -d "$ORIG" ]; then
	mkdir -p "$ORIG"
	for f in /usr/local/bin/fp5-fprintd /etc/systemd/system/$UNIT /etc/pam.d/fp5-fprint-test \
		/etc/dbus-1/system.d/fp5-fprintd.conf "$POLICY"; do
		if S test -e "$f"; then S cp -a "$f" "$ORIG/$(echo "$f" | tr / _)"; fi
	done
	S test -e "$DROPIN" && echo had-dropin > "$ORIG/had-dropin" || true
	S test -e "$POLICY" && echo yes > "$ORIG/had-policy" || true
	systemctl is-enabled $UNIT > "$ORIG/enabled" 2>&1 || true
	S chown -R "$(id -u):$(id -g)" "$ORIG"
fi

# 3. Install.
S install -d -m 755 $LIB
S install -m 755 "$K/fp5_fprintd.py" $LIB/fp5_fprintd.py
S install -m 755 "$K/fp5-pam-test.py" /usr/local/bin/fp5-pam-test
S install -d -m 755 /etc/systemd/system/$UNIT.d
S install -m 644 "$K/fp5-fprintd-qtee.conf" "$DROPIN"
S install -m 644 "$K/pam-fp5-fprint-test" /etc/pam.d/fp5-fprint-test
[ -f "$ORIG/had-policy" ] || S install -m 644 "$K/net.reactivated.fprint.device.policy" "$POLICY"
S install -d -m 755 /var/lib/fp5-qtee
S systemctl daemon-reload
S systemctl restart $UNIT
sleep 2
systemctl is-active $UNIT
busctl --system call net.reactivated.Fprint /net/reactivated/Fprint/Manager \
	net.reactivated.Fprint.Manager GetDefaultDevice
busctl --system call net.reactivated.Fprint /net/reactivated/Fprint/Device/0 \
	net.reactivated.Fprint.Device ListEnrolledFingers s user
echo "deployed. restore: $K/restore.sh"
