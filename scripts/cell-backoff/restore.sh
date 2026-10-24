#!/bin/sh
# Undo deploy.sh: old resume script back (reattach on every resume), no
# backoff, no imsd gate, NM autoconnect on qrtr0 back on. No reboot.
set -u
K=$(cd "$(dirname "$0")" && pwd); O=$K/orig; S() { sudo -n "$@"; }
S systemctl disable --now fp5-cell-backoff.timer 2>/dev/null
# a pending suspend re-arm (transient unit from systemd-run)
S systemctl stop fp5-cell-rearm.timer fp5-cell-rearm.service 2>/dev/null
S systemctl reset-failed fp5-cell-rearm.service 2>/dev/null
S rm -f /etc/systemd/system/fp5-cell-backoff.timer /etc/systemd/system/fp5-cell-backoff.service \
	/etc/systemd/system/imsd.service.d/fp5-cell-gate.conf /usr/lib/systemd/system-sleep/fp5-cell-presleep
S rmdir /etc/systemd/system/imsd.service.d 2>/dev/null
[ -f "$O/fp5-telephony-resume.sh" ] && S install -m 755 "$O/fp5-telephony-resume.sh" /usr/local/bin/fp5-telephony-resume.sh
S rm -f /usr/local/bin/fp5-cell-backoff
S rm -rf /var/lib/fp5-cell
S systemctl daemon-reload
nmcli device set qrtr0 autoconnect yes 2>/dev/null || S nmcli device set qrtr0 autoconnect yes
S systemctl start --no-block imsd.service
echo restored
