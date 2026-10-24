#!/bin/sh
# Deploy fp5-cell-backoff (suspend-safe mobile-data retries). Run on the phone
# from this directory as the user (sudo -n). No reboot. Undo: ./restore.sh
set -eu
K=$(cd "$(dirname "$0")" && pwd); O=$K/orig; S() { sudo -n "$@"; }
for f in fp5-cell-backoff fp5-telephony-resume.sh fp5-cell-presleep imsd-fp5-cell-gate.conf fp5-cell-backoff.service fp5-cell-backoff.timer; do
	[ -f "$K/$f" ] || { echo "missing $f"; exit 1; }
done
sh -n "$K/fp5-cell-backoff"; sh -n "$K/fp5-telephony-resume.sh"
mkdir -p "$O"
if [ ! -f "$O/saved" ]; then
	cp -a /usr/local/bin/fp5-telephony-resume.sh "$O/"
	nmcli -g GENERAL.AUTOCONNECT device show qrtr0 > "$O/qrtr0-autoconnect" 2>/dev/null || true
	date -u +%FT%TZ > "$O/saved"
fi
S install -m 755 "$K/fp5-cell-backoff" /usr/local/bin/fp5-cell-backoff
S install -m 755 "$K/fp5-telephony-resume.sh" /usr/local/bin/fp5-telephony-resume.sh
S install -m 755 "$K/fp5-cell-presleep" /usr/lib/systemd/system-sleep/fp5-cell-presleep
S install -d /etc/systemd/system/imsd.service.d
S install -m 644 "$K/imsd-fp5-cell-gate.conf" /etc/systemd/system/imsd.service.d/fp5-cell-gate.conf
S install -m 644 "$K/fp5-cell-backoff.service" "$K/fp5-cell-backoff.timer" /etc/systemd/system/
S systemctl daemon-reload
S systemctl enable --now fp5-cell-backoff.timer
S /usr/local/bin/fp5-cell-backoff timer || true
/usr/local/bin/fp5-cell-backoff status
echo "deployed; restore: $K/restore.sh"
