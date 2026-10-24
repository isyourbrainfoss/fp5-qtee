#!/bin/sh
# After resume (or boot): re-arm paging wakes + LTE + hangup + SMS retention.
# Install: /usr/local/bin/fp5-telephony-resume.sh + systemd unit below.
set -u
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
log "=== fp5-telephony-resume ==="
# runs from both the system-sleep hook and fp5-telephony-resume.service: once is enough
exec 8>/run/fp5-telephony-resume.lock
flock -n 8 || { log "already running"; exit 0; }

# chatty can race-consume SMS in MM
pkill -x chatty 2>/dev/null || true
pkill -x chatty-daemon 2>/dev/null || true

mmcli -m 0 --voice-hangup-all 2>/dev/null || true
if [ -x /usr/local/bin/fp5-call-resleep-ready.sh ]; then
	/usr/local/bin/fp5-call-resleep-ready.sh || true
fi

# IPA only. smp2p/remoteproc device wakes abort the next settle (SMEM poll is enough).
[ -f /sys/devices/platform/soc@0/1e40000.ipa/power/wakeup ] \
  && echo enabled > /sys/devices/platform/soc@0/1e40000.ipa/power/wakeup 2>/dev/null || true
for p in \
  /sys/devices/platform/soc@0/3700000.remoteproc/power/wakeup \
  /sys/devices/platform/soc@0/4080000.remoteproc/power/wakeup \
  /sys/devices/platform/soc@0/8a00000.remoteproc/power/wakeup \
  /sys/devices/platform/soc@0/a300000.remoteproc/power/wakeup \
  /sys/devices/platform/smp2p-mpss/power/wakeup \
  /sys/devices/platform/smp2p-wpss/power/wakeup \
  /sys/devices/platform/smp2p-adsp/power/wakeup \
  /sys/devices/platform/smp2p-cdsp/power/wakeup
do
  [ -f "$p" ] && echo disabled >"$p" 2>/dev/null || true
done
# Prefer IPA not runtime-suspended between pages
[ -f /sys/devices/platform/soc@0/1e40000.ipa/power/control ] \
  && echo on > /sys/devices/platform/soc@0/1e40000.ipa/power/control 2>/dev/null || true

# Mobile data: retry with backoff instead of a full reattach (and a
# ModemManager restart) on every resume. fp5-cell-backoff decides; with no
# packet service (e.g. prepaid out of balance) it keeps retries quiet so they
# cannot wake the phone again. Old behaviour: FP5_RESUME_REATTACH=1.
if [ "${FP5_RESUME_REATTACH:-0}" = 1 ] && [ -x /usr/local/bin/fp5-lte-reattach.sh ]; then
  DO_PING="${DO_PING:-0}" /usr/local/bin/fp5-lte-reattach.sh || true
elif [ -x /usr/local/bin/fp5-cell-backoff ]; then
  /usr/local/bin/fp5-cell-backoff resume || true
else
  mmcli -m 0 -e 2>/dev/null || true
fi

# Snapshot wake + modem for lab dumps
{
  echo "IPA=$(cat /sys/devices/platform/soc@0/1e40000.ipa/power/wakeup 2>/dev/null)"
  mmcli -m any 2>&1 | grep -E "state:|packet|operator|registration" || true
} | while read -r line; do log "$line"; done

log "TELEPHONY_RESUME_OK"
