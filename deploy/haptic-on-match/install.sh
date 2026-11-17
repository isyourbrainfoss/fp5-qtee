#!/bin/sh
# Install the haptic-on-match watcher into the login-pin copy on BengalOS.
# Run as root (run0) from the stage dir. Restarts only the fp5 user unit
# fp5-qtee-unlock.service. Undo: rollback.sh in the same dir.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DST=/var/lib/fp5-qtee-loginpin/fp5_qtee_unlock.py
BAK="$HERE/fp5_qtee_unlock.py.orig"
[ -f "$BAK" ] || cp -a "$DST" "$BAK"
(cd "$HERE" && sha256sum -c SHA256SUMS)
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$HERE/fp5_qtee_unlock.py"
install -m 0644 -o root -g root "$HERE/fp5_qtee_unlock.py" "$DST.new"
mv -f "$DST.new" "$DST"
run0 --no-ask-password -u fp5 env XDG_RUNTIME_DIR=/run/user/60434 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/60434/bus \
  systemctl --user restart fp5-qtee-unlock.service
sleep 2
run0 --no-ask-password -u fp5 env XDG_RUNTIME_DIR=/run/user/60434 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/60434/bus \
  systemctl --user is-active fp5-qtee-unlock.service
sha256sum "$DST"
