#!/bin/sh
# Deploy the fingerprint UI pieces. Run on the phone from the kit dir as the
# user (uses sudo -n). Does NOT touch fingerprint storage, the unlock watcher,
# PAM files or the sensor. No reboot. Undo with ./restore.sh.
#
#   1. GNOME Settings shim: /usr/local/share/glib-2.0/schemas/org.gnome.login-screen.gschema.xml
#      (makes Settings -> System -> Users show "Fingerprint Login")
#   2. Fingerprints app: ~/.local/bin/fp5-fingerprints + desktop entry
#   3. fp5-fprintd with the org.fp5.Fprint.Manage extension (rename);
#      the old file is saved in orig/ and the service restarted.
set -eu
K=$(cd "$(dirname "$0")" && pwd)
ORIG=$K/orig
LIB=/usr/local/lib/fp5-qtee
SCH=/usr/local/share/glib-2.0/schemas
S() { sudo -n "$@"; }
for f in fp5_fprintd.py fp5_fingerprints.py org.fp5.Fingerprints.desktop org.gnome.login-screen.gschema.xml; do
	[ -f "$K/$f" ] || { echo "missing $K/$f"; exit 1; }
done
python3 -m py_compile "$K/fp5_fprintd.py" "$K/fp5_fingerprints.py"
mkdir -p "$ORIG"
if [ ! -f "$ORIG/saved" ]; then
	S cp -a $LIB/fp5_fprintd.py "$ORIG/fp5_fprintd.py"
	S sh -c 'test -f /var/lib/fp5-qtee/fingers.json && cp -a /var/lib/fp5-qtee/fingers.json '"$ORIG"'/fingers.json' || true
	S chown -R "$(id -u):$(id -g)" "$ORIG"
	[ -d $SCH ] && echo had-schema-dir > "$ORIG/had-schema-dir"
	echo "$(date -u +%FT%TZ)" > "$ORIG/saved"
fi
# 1. schema shim
S install -d -m 755 $SCH
S install -m 644 "$K/org.gnome.login-screen.gschema.xml" $SCH/
S glib-compile-schemas $SCH
# 2. app
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
install -m 755 "$K/fp5_fingerprints.py" "$HOME/.local/bin/fp5-fingerprints"
sed "s|@HOME@|$HOME|" "$K/org.fp5.Fingerprints.desktop" > "$HOME/.local/share/applications/org.fp5.Fingerprints.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
# 3. daemon (service restart: storage and sensor are not touched; PAM falls
#    back to the password for the second it takes)
S install -m 755 "$K/fp5_fprintd.py" $LIB/fp5_fprintd.py
S systemctl restart fp5-fprintd.service
sleep 2
systemctl is-active fp5-fprintd.service
busctl --system call net.reactivated.Fprint /net/reactivated/Fprint/Device/0 \
	net.reactivated.Fprint.Device ListEnrolledFingers s user
busctl --system call net.reactivated.Fprint /net/reactivated/Fprint/Device/0 \
	org.fp5.Fprint.Manage ListDetailed s user
echo "schema: $(gsettings get org.gnome.login-screen enable-fingerprint-authentication)"
echo "deployed. restore: $K/restore.sh"
