#!/bin/sh
# Undo deploy.sh: back to the old fp5-fprintd stub and no test PAM service.
# Fingerprint storage is NOT restored automatically; see the end.
set -u
K=$(cd "$(dirname "$0")" && pwd)
ORIG=$K/orig
UNIT=fp5-fprintd.service
DROPIN=/etc/systemd/system/$UNIT.d/zz-qtee.conf
POLICY=/usr/share/polkit-1/actions/net.reactivated.fprint.device.policy
S() { sudo -n "$@"; }
S rm -f "$DROPIN" /usr/local/bin/fp5-pam-test
S rmdir /etc/systemd/system/$UNIT.d 2>/dev/null || true
if [ -f "$ORIG/_etc_pam.d_fp5-fprint-test" ]; then
	S cp -a "$ORIG/_etc_pam.d_fp5-fprint-test" /etc/pam.d/fp5-fprint-test
else
	S rm -f /etc/pam.d/fp5-fprint-test
fi
[ -f "$ORIG/had-policy" ] || S rm -f "$POLICY"
S systemctl daemon-reload
S systemctl restart $UNIT
systemctl is-active $UNIT
echo "restored old fp5-fprintd (stub). /usr/local/lib/fp5-qtee/fp5_fprintd.py left in place (unused)."
echo "Storage backup taken at deploy: $(cat "$K/STORAGE-BACKUP" 2>/dev/null)"
echo "Only if fingers were lost: sudo cp -a <backup>/misc/. /mnt/persist/misc/ (watcher stopped), then check fp5-qtee list."
