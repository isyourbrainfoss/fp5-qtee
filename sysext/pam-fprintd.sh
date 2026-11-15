#!/bin/sh
# Put Debian's pam_fprintd.so (package libpam-fprintd, arm64) into a sysext
# staging tree, so polkit/run0 prompts can ask fp5-fprintd for a finger.
#   sysext/pam-fprintd.sh DESTDIR
# Only the PAM module and its copyright are taken. Not the fprintd daemon
# (fp5-fprintd owns net.reactivated.Fprint) and not
# /usr/share/pam-configs/fprintd (pam-auth-update would then add it to
# common-auth, i.e. to every login, including the Phosh lock screen).
# Nothing goes to /etc: see pam/polkit-1.example and
# docs/bengalos-pam-fprintd.md for the opt-in PAM file.
#
# Pinned to Debian forky/sid, the base of BengalOS 0.26.1114.1
# (glibc 2.43-7, libpam 1.7.0, libsystemd.so.0.45, polkitd 128-1).
# The module needs GLIBC_2.38, LIBSYSTEMD_237 and LIBPAM_1.0.
# The .deb is fetched once into $FP5_DEB_CACHE (default sysext/cache/) and
# checked against the sha256 below before it is used.
set -eu
dest=${1:?DESTDIR}
here=$(cd "$(dirname "$0")" && pwd)
cache=${FP5_DEB_CACHE:-$here/cache}

PKG=libpam-fprintd
VER=1.94.5-4+b2
DEB=libpam-fprintd_1.94.5-4+b2_arm64.deb
SHA256=9497ad300aba9f44d6674547666a026afb42568402f6ff49f2f56cebb39988de
# deb.debian.org drops the file once forky moves on; snapshot keeps it
# (snapshot file id = the .deb's sha1).
URLS="https://snapshot.debian.org/file/581c21892b791a9e23ca94b4bcd21f484ead9e46
https://deb.debian.org/debian/pool/main/f/fprintd/$DEB"

mkdir -p "$cache"
deb="$cache/$DEB"
ok() { [ -f "$deb" ] && echo "$SHA256  $deb" | sha256sum -c --status; }
if ! ok; then
	for u in $URLS; do
		rm -f "$deb.part"
		if curl -fsSL --retry 3 -o "$deb.part" "$u"; then
			mv "$deb.part" "$deb"
			ok && break
		fi
		echo "pam-fprintd: $u failed or sha256 mismatch" >&2
		rm -f "$deb" "$deb.part"
	done
fi
ok || { echo "pam-fprintd: no verified $DEB" >&2; exit 1; }

x=$(mktemp -d)
trap 'rm -rf "$x"' EXIT
if command -v dpkg-deb >/dev/null 2>&1; then
	dpkg-deb -x "$deb" "$x"
else
	# No dpkg-deb (e.g. an Arch host): same result from the ar member.
	(cd "$x" && ar x "$deb" && tar -xf data.tar.* && rm -f data.tar.* control.tar.* debian-binary)
fi
so=usr/lib/aarch64-linux-gnu/security/pam_fprintd.so
[ -f "$x/$so" ] || { echo "pam-fprintd: $so missing in $DEB" >&2; exit 1; }
install -Dm644 "$x/$so" "$dest/$so"
install -Dm644 "$x/usr/share/doc/$PKG/copyright" "$dest/usr/share/doc/$PKG/copyright"
printf '%s %s %s\n' "$PKG" "$VER" "$SHA256" > "$dest/usr/share/doc/$PKG/fp5-qtee-source"
echo "$dest/$so"
