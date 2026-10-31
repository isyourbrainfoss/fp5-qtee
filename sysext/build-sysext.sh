#!/bin/sh
# Build a systemd-sysext image of fp5-qtee for BengalOS (aarch64).
#   sysext/build-sysext.sh VERSION_ID OUT.raw
# VERSION_ID must match /etc/os-release on the phone (e.g. 0.26.1027.1),
# or pass _any to skip the version check (then also needs ID=_any or
# matching ID; we keep ID=bengalos).
# Install on the phone (owner step):
#   cp OUT.raw /var/lib/extensions/fp5-qtee.raw && systemd-sysext refresh
#   systemctl daemon-reload && systemctl enable --now fp5-qtee.socket
#   busctl call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus ReloadConfig
set -eu
ver=${1:?VERSION_ID}
out=${2:?OUT.raw}
here=$(cd "$(dirname "$0")/.." && pwd)
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
make -C "$here/session" clean >/dev/null
make -C "$here/session" all CC=${CC:-aarch64-linux-gnu-gcc} CFLAGS="${CFLAGS:--O2 -Wall -D_FORTIFY_SOURCE=2 -fstack-protector-strong}" >/dev/null
make -C "$here/service" install DESTDIR="$stage" >/dev/null
rel="$stage/usr/lib/extension-release.d/extension-release.fp5-qtee"
mkdir -p "$(dirname "$rel")"
if [ "$ver" = _any ]; then
	printf 'ID=_any\nARCHITECTURE=arm64\nEXTENSION_RELOAD_MANAGER=1\n' > "$rel"
else
	printf 'ID=bengalos\nVERSION_ID=%s\nARCHITECTURE=arm64\nEXTENSION_RELOAD_MANAGER=1\n' "$ver" > "$rel"
fi
# The image must hold only /usr (and /opt). No /etc: config stays in confext.
[ -z "$(ls -A "$stage" | grep -v '^usr$')" ]
rm -f "$out"
mkfs.erofs -zlz4hc -T0 --all-root "$out" "$stage" >/dev/null
echo "$out"
