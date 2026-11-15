#!/bin/sh
# Build a systemd-sysext image of fp5-qtee for BengalOS (aarch64).
#   sysext/build-sysext.sh VERSION_ID OUT.raw
# VERSION_ID must match /etc/os-release on the phone (e.g. 0.26.1027.1),
# or pass _any to skip the version check (then also needs ID=_any or
# matching ID; we keep ID=bengalos).
# extension-release is named after OUT (minus .raw), as systemd-sysext
# requires: fp5-qtee.raw -> extension-release.fp5-qtee, the feed's
# fp5-qtee_0.26.1114.1.raw -> extension-release.fp5-qtee_0.26.1114.1.
# Its fields match the feed's 0.26.1114.1 image (ID, VERSION_ID,
# ARCHITECTURE, SYSEXT_SCOPE=system, EXTENSION_RELOAD_MANAGER=1).
# The image also carries Debian's pam_fprintd.so (sysext/pam-fprintd.sh,
# pinned .deb + sha256; needs network once, then sysext/cache/).
# FP5_SYSEXT_PAM_FPRINTD=0 leaves it out.
# Install on the phone (owner step):
#   sysext/install-etc.sh   # polkit after the merge (/etc drop-in, once)
#   cp OUT.raw /var/lib/extensions/fp5-qtee.raw && systemd-sysext refresh
#   (build OUT as fp5-qtee.raw then: extension-release follows the name)
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
if [ "${FP5_SYSEXT_PAM_FPRINTD:-1}" != 0 ]; then
	"$here/sysext/pam-fprintd.sh" "$stage" >/dev/null
fi
name=$(basename "$out" .raw)
rel="$stage/usr/lib/extension-release.d/extension-release.$name"
mkdir -p "$(dirname "$rel")"
if [ "$ver" = _any ]; then
	printf 'ID=_any\nARCHITECTURE=arm64\nSYSEXT_SCOPE=system\nEXTENSION_RELOAD_MANAGER=1\n' > "$rel"
else
	printf 'ID=bengalos\nVERSION_ID=%s\nARCHITECTURE=arm64\nSYSEXT_SCOPE=system\nEXTENSION_RELOAD_MANAGER=1\n' "$ver" > "$rel"
fi
# The image must hold only /usr (and /opt). No /etc: config stays in confext.
[ -z "$(ls -A "$stage" | grep -v '^usr$')" ]
rm -f "$out"
mkfs.erofs -zlz4hc -T0 --all-root "$out" "$stage" >/dev/null
echo "$out"
