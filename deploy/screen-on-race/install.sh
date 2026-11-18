#!/bin/sh
# Screen-on race fix for the BengalOS login-pin watcher (fix/screen-on-race).
# Run as root (run0) from this dir. Replaces only
# /var/lib/fp5-qtee-loginpin/fp5_qtee_unlock.py and restarts only the fp5
# user unit fp5-qtee-unlock.service. Undo: rollback.sh in this dir.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DIR=/var/lib/fp5-qtee-loginpin
cd "$HERE"
sha256sum -c SHA256SUMS
# Refuse if the installed copy is not the one this fix was made against.
grep -q " installed-base$" BASE.sha256 && \
  [ "$(sha256sum "$DIR/fp5_qtee_unlock.py" | cut -d' ' -f1)" = "$(cut -d' ' -f1 BASE.sha256)" ] || {
  echo "installed fp5_qtee_unlock.py is not the expected base; not installing" >&2; exit 1; }
[ -f fp5_qtee_unlock.py.orig ] || cp -a "$DIR/fp5_qtee_unlock.py" fp5_qtee_unlock.py.orig
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" fp5_qtee_unlock.py
install -m 0644 -o root -g root fp5_qtee_unlock.py "$DIR/fp5_qtee_unlock.py.new"
mv -f "$DIR/fp5_qtee_unlock.py.new" "$DIR/fp5_qtee_unlock.py"
rm -rf "$DIR/__pycache__"
U="run0 --no-ask-password -u fp5 env XDG_RUNTIME_DIR=/run/user/60434 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/60434/bus"
$U systemctl --user restart fp5-qtee-unlock.service
sleep 2
$U systemctl --user is-active fp5-qtee-unlock.service
sha256sum "$DIR"/fp5_qtee_*.py
