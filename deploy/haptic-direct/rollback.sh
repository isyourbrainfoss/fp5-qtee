#!/bin/sh
# Put back the watcher files from before install.sh; restart the fp5 user unit.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DIR=/var/lib/fp5-qtee-loginpin
cd "$HERE"
install -m 0644 -o root -g root fp5_qtee_unlock.py.orig "$DIR/fp5_qtee_unlock.py.new"
mv -f "$DIR/fp5_qtee_unlock.py.new" "$DIR/fp5_qtee_unlock.py"
if [ -f fp5_qtee_haptic.py.orig ]; then
  install -m 0644 -o root -g root fp5_qtee_haptic.py.orig "$DIR/fp5_qtee_haptic.py"
else
  rm -f "$DIR/fp5_qtee_haptic.py"   # was absent: /usr/share/fp5-qtee copy is used again
fi
rm -rf "$DIR/__pycache__"
U="run0 --no-ask-password -u fp5 env XDG_RUNTIME_DIR=/run/user/60434 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/60434/bus"
$U systemctl --user restart fp5-qtee-unlock.service
sleep 2
$U systemctl --user is-active fp5-qtee-unlock.service
sha256sum "$DIR"/fp5_qtee_*.py
