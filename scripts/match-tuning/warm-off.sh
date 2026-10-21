#!/bin/sh
# Undo warm-on.sh.
set -eu
rm -f "$HOME/.config/systemd/user/fp5-qtee-unlock.service.d/warm.conf"
systemctl --user daemon-reload
systemctl --user restart fp5-qtee-unlock.service
echo "warm mode off"
