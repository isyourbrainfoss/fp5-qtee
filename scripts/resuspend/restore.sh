#!/bin/sh
# Put the stock gsd-power back and restart it.
set -eu
K=${K:-/home/user/fp5-qtee-keep/resuspend-20261023}
[ -f "$K/orig/gsd-power" ] || { echo "no backup in $K/orig"; exit 1; }
sudo cp "$K/orig/gsd-power" /usr/libexec/gsd-power.new && sudo chmod 755 /usr/libexec/gsd-power.new && sudo mv /usr/libexec/gsd-power.new /usr/libexec/gsd-power
sha256sum /usr/libexec/gsd-power; cat "$K/orig/gsd-power.sha256"
sh "$K/restart-gsd-power.sh"
