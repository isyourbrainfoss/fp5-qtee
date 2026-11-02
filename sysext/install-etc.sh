#!/bin/sh
# Install the /etc drop-ins the fp5-qtee sysext cannot ship itself.
#   sysext/install-etc.sh [ROOT]    (owner step, as root; ROOT defaults to /)
# A sysext only holds /usr, and a drop-in under /usr inside the image is
# read only after the merge. polkit must be ordered after the merge from
# the first boot, so its drop-in goes to /etc.
# Revert: rm /etc/systemd/system/polkit.service.d/10-after-sysext.conf
#         systemctl daemon-reload
set -eu
root=${1:-/}
here=$(cd "$(dirname "$0")/.." && pwd)
dir="$root/etc/systemd/system/polkit.service.d"
install -d -m 0755 "$dir"
install -m 0644 "$here/service/polkit-after-sysext.conf" "$dir/10-after-sysext.conf"
if [ "$root" = / ] && command -v systemctl >/dev/null 2>&1; then
	systemctl daemon-reload
fi
echo "$dir/10-after-sysext.conf"
