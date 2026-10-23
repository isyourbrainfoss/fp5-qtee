#!/bin/sh
# Build the patched gsd-power ON THE PHONE in an isolated apk root (nothing is
# installed into the phone's own rootfs; delete $R afterwards).
#   sudo sh build-gsd-power.sh      (needs gnome-settings-daemon-51.0.tar.xz in $K)
# Result: $K/gsd-power.patched (prefix /usr, RUNPATH /usr/lib/gnome-settings-daemon-51)
set -eu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
K=${K:-/home/user/fp5-qtee-keep/resuspend-20261023}
R=${R:-/home/user/gsdbuild}
mkdir -p $R/etc/apk $R/dev $R/proc $R/src
cp /etc/apk/repositories $R/etc/apk/
apk add --root $R --initdb --keys-dir /etc/apk/keys --no-scripts -U \
  build-base meson samurai pkgconf bash coreutils libxml2-utils \
  glib-dev gtk+3.0-dev upower-dev libnotify-dev libcanberra-dev gnome-desktop-dev \
  gsettings-desktop-schemas-dev alsa-lib-dev colord-dev cups-dev elogind-dev geoclue-dev \
  geocode-glib-dev libgweather4-dev libwacom-dev modemmanager-dev networkmanager-dev \
  pango-dev polkit-dev pulseaudio-dev gcr4-dev lcms2-dev libxkbcommon-dev
tar -xf $K/gnome-settings-daemon-51.0.tar.xz --strip-components=1 -C $R/src
patch -d $R/src -p1 < "$(dirname "$0")/0001-power-reset-idle-mode-on-resume.patch"
mount --bind /dev $R/dev; mount -t proc proc $R/proc
trap 'umount $R/proc $R/dev' EXIT
chroot $R /bin/busybox --install -s /bin
# --no-scripts leaves busybox applets only; coreutils needs its multicall links (ln --relative)
for f in $(apk info --root $R -L coreutils | grep '^bin/' | cut -d/ -f2); do [ $f = coreutils ] || ln -sf coreutils $R/bin/$f; done
chroot $R /bin/sh -c 'cd /src && meson setup out -Dbuildtype=release -Db_lto=false -Dprefix=/usr -Dlibdir=lib -Dlibexecdir=libexec -Dsystemd=false -Dsystemd-units=true -Delogind=true \
  && ninja -C out && rm -rf /stage && DESTDIR=/stage meson install -C out --no-rebuild >/dev/null && cp /stage/usr/libexec/gsd-power /gsd-power.patched'
cp $R/gsd-power.patched $K/gsd-power.patched
echo built $K/gsd-power.patched
