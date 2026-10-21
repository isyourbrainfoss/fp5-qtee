#!/bin/sh
# Optional step C: keep one "serve" session loaded while locked
# (FP5_QTEE_WARM=1), so a wake or a re-press after a miss does not pay
# the 1.5-3 s trustlet load and calibration again. Watcher setting only.
set -eu
U=$HOME/.config/systemd/user/fp5-qtee-unlock.service.d
mkdir -p "$U"
printf '[Service]\nEnvironment=FP5_QTEE_WARM=1\n' > "$U/warm.conf"
systemctl --user daemon-reload
systemctl --user restart fp5-qtee-unlock.service
echo "warm mode on"
