#!/bin/sh
# Undo deploy.sh. Fingerprint storage was never touched. fingers.json is left
# as it is (a rename only changes names; the saved copy is in orig/fingers.json
# if you want the pre-deploy names back: sudo cp orig/fingers.json /var/lib/fp5-qtee/).
set -u
K=$(cd "$(dirname "$0")" && pwd)
ORIG=$K/orig
S() { sudo -n "$@"; }
S rm -f /usr/local/share/glib-2.0/schemas/org.gnome.login-screen.gschema.xml
if [ -d /usr/local/share/glib-2.0/schemas ]; then
	if [ -f "$ORIG/had-schema-dir" ]; then S glib-compile-schemas /usr/local/share/glib-2.0/schemas
	else S rm -f /usr/local/share/glib-2.0/schemas/gschemas.compiled; S rmdir /usr/local/share/glib-2.0/schemas /usr/local/share/glib-2.0 2>/dev/null; fi
fi
rm -f "$HOME/.local/bin/fp5-fingerprints" "$HOME/.local/share/applications/org.fp5.Fingerprints.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null
if [ -f "$ORIG/fp5_fprintd.py" ]; then
	S install -m 755 "$ORIG/fp5_fprintd.py" /usr/local/lib/fp5-qtee/fp5_fprintd.py
	S systemctl restart fp5-fprintd.service
	sleep 1; systemctl is-active fp5-fprintd.service
fi
echo "restored."
