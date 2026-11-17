#!/bin/sh
# Direct aw86927 fingerprint haptic for the BengalOS login-pin watcher.
# Run as root (run0) from this dir. Installs fp5_qtee_unlock.py and
# fp5_qtee_haptic.py into /var/lib/fp5-qtee-loginpin (first on the unit's
# PYTHONPATH) and restarts only the fp5 user unit fp5-qtee-unlock.service.
# Undo: rollback.sh in this dir.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DIR=/var/lib/fp5-qtee-loginpin
cd "$HERE"
sha256sum -c SHA256SUMS
[ -f fp5_qtee_unlock.py.orig ] || cp -a "$DIR/fp5_qtee_unlock.py" fp5_qtee_unlock.py.orig
[ -e fp5_qtee_haptic.py.orig ] || [ -e fp5_qtee_haptic.py.absent ] || {
  if [ -f "$DIR/fp5_qtee_haptic.py" ]; then cp -a "$DIR/fp5_qtee_haptic.py" fp5_qtee_haptic.py.orig
  else : > fp5_qtee_haptic.py.absent; fi; }
for f in fp5_qtee_unlock.py fp5_qtee_haptic.py; do
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$f"
  install -m 0644 -o root -g root "$f" "$DIR/$f.new"
  mv -f "$DIR/$f.new" "$DIR/$f"
done
rm -rf "$DIR/__pycache__"
U="run0 --no-ask-password -u fp5 env XDG_RUNTIME_DIR=/run/user/60434 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/60434/bus"
$U systemctl --user restart fp5-qtee-unlock.service
sleep 2
$U systemctl --user is-active fp5-qtee-unlock.service
sha256sum "$DIR"/fp5_qtee_*.py
