#!/bin/sh
# Put back the watcher that was there before install.sh and restart the unit.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DST=/var/lib/fp5-qtee-loginpin/fp5_qtee_unlock.py
install -m 0644 -o root -g root "$HERE/fp5_qtee_unlock.py.orig" "$DST.new"
mv -f "$DST.new" "$DST"
run0 --no-ask-password -u fp5 env XDG_RUNTIME_DIR=/run/user/60434 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/60434/bus \
  systemctl --user restart fp5-qtee-unlock.service
sha256sum "$DST"
