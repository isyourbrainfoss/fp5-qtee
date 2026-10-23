#!/bin/sh
# Install the patched gsd-power (idle mode reset on resume) -- reversible.
#   deploy.sh [path/to/gsd-power.patched]
# Backs up the stock /usr/libexec/gsd-power once, installs the patched binary,
# and restarts gsd-power (SIGKILL; systemd Restart=on-failure respawns it in
# ~100 ms, the session is not affected). No reboot needed.
set -eu
K=${K:-/home/user/fp5-qtee-keep/resuspend-20261023}
NEW=${1:-$K/gsd-power.patched}
BIN=/usr/libexec/gsd-power
[ -x "$NEW" ] || { echo "missing $NEW"; exit 1; }
mkdir -p "$K/orig"
if [ ! -f "$K/orig/gsd-power" ]; then
	sudo cp -p "$BIN" "$K/orig/gsd-power"
	sha256sum "$K/orig/gsd-power" | tee "$K/orig/gsd-power.sha256"
fi
# sanity: must resolve all libs before we swap it in
ldd "$NEW" | grep -q "not found" && { echo "unresolved libs"; ldd "$NEW"; exit 1; }
sudo cp "$NEW" "$BIN.new" && sudo chmod 755 "$BIN.new" && sudo mv "$BIN.new" "$BIN"
echo "installed: $(sha256sum "$BIN")"
sh "$K/restart-gsd-power.sh"
