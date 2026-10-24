#!/bin/sh
# Desk test for fp5-upgrade-guard in a fake root. No phone needed.
set -u
G=$(cd "$(dirname "$0")" && pwd)/fp5-upgrade-guard
R=$(mktemp -d); trap 'rm -rf "$R"' EXIT
export FP5_ROOT=$R FP5_KREL=7.2.9
pass=0; failn=0
t() { if eval "$2"; then pass=$((pass+1)); else echo "not ok: $1"; failn=$((failn+1)); fi; }

db() { # name ver [dir file]...
	n=$1 v=$2; shift 2
	{ echo "P:$n"; echo "V:$v"; while [ $# -ge 2 ]; do echo "F:$1"; echo "R:$2"; shift 2; done; echo; } >> "$R/lib/apk/db/installed"
}
mkdir -p "$R/lib/apk/db" "$R/etc/apk" "$R/usr/libexec" "$R/usr/local/bin" "$R/etc/systemd/system/imsd.service.d" \
	"$R/lib/modules/7.2.9/extra" "$R/home/user/fp5-qtee-keep/resuspend-20261023"
: > "$R/lib/apk/db/installed"
db gnome-settings-daemon 51.0-r0 usr/libexec gsd-power
db linux-postmarketos-qcom-sc7280 7.2.9-r0 lib/modules/7.2.9 modules.order
printf 'gnome-settings-daemon\nphosh\n' > "$R/etc/apk/world"
echo 'deviceinfo_flash_kernel_on_update="false"' > "$R/etc/deviceinfo"
echo PATCHED > "$R/usr/libexec/gsd-power"; cp "$R/usr/libexec/gsd-power" "$R/home/user/fp5-qtee-keep/resuspend-20261023/gsd-power.patched"
echo backoff > "$R/usr/local/bin/fp5-cell-backoff"; chmod 755 "$R/usr/local/bin/fp5-cell-backoff"
echo gate > "$R/etc/systemd/system/imsd.service.d/fp5-cell-gate.conf"
echo ko > "$R/lib/modules/7.2.9/extra/focaltech_fp_life.ko"
echo dep > "$R/lib/modules/7.2.9/modules.dep"

out=$("$G" snapshot); t "snapshot lists 4 files" 'echo "$out" | grep -q "snapshot: 4 files"'
out=$("$G" check); rc=$?
t "clean check exit 1 (warn: modules owned by kernel pkg)" '[ $rc = 1 ]'
t "clean check: gsd ok" 'echo "$out" | grep -q "^OK    gsd-power is the patched"'
t "clean check: warns kernel pkg owns modules" 'echo "$out" | grep -q "owned by apk package linux-postmarketos-qcom-sc7280"'

# apk upgrade of the same g-s-d version put the stock binary back, a drop-in vanished
echo STOCK > "$R/usr/libexec/gsd-power"; rm "$R/etc/systemd/system/imsd.service.d/fp5-cell-gate.conf"
out=$("$G" check); rc=$?
t "stock gsd detected (exit 2)" '[ $rc = 2 ] && echo "$out" | grep -q "^FAIL  gsd-power is stock (same package"'
t "missing drop-in detected" 'echo "$out" | grep -q "^FAIL  /etc/systemd/system/imsd.service.d/fp5-cell-gate.conf missing"'
out=$("$G" fix); rc=$?
t "fix re-installs gsd" 'grep -q PATCHED "$R/usr/libexec/gsd-power" && echo "$out" | grep -q "^FIXED gsd-power"'
t "fix restores drop-in" '[ "$(cat "$R/etc/systemd/system/imsd.service.d/fp5-cell-gate.conf")" = gate ]'
t "exec bit kept" '[ -x "$R/usr/local/bin/fp5-cell-backoff" ]'
out=$("$G" check); rc=$?; t "after fix: no FAIL" '! echo "$out" | grep -q "^FAIL"'

# g-s-d upgraded to a new version: must not install the 51.0 binary over 52.0
sed -i 's/^V:51.0-r0/V:52.0-r0/' "$R/lib/apk/db/installed"; echo STOCK52 > "$R/usr/libexec/gsd-power"
out=$("$G" fix); rc=$?
t "new g-s-d version: refuses to re-apply" 'grep -q STOCK52 "$R/usr/libexec/gsd-power" && echo "$out" | grep -q "rebuild it"'

# kernel package upgraded while running the shared 7.2.9 release
sed -i 's/^V:7.2.9-r0/V:7.2.9-r1/' "$R/lib/apk/db/installed"
out=$("$G" check); t "kernel pkg change on shared release = FAIL" 'echo "$out" | grep -q "^FAIL  distro kernel package changed 7.2.9-r0 -> 7.2.9-r1"'
rm "$R/lib/modules/7.2.9/extra/focaltech_fp_life.ko"
out=$("$G" check); t "missing OOT module = FAIL" 'echo "$out" | grep -q "^FAIL  out-of-tree module /lib/modules/7.2.9/extra/focaltech_fp_life.ko"'
"$G" fix >/dev/null; t "fix restores OOT module" '[ -f "$R/lib/modules/7.2.9/extra/focaltech_fp_life.ko" ]'

# custom release: no ownership warning, pkg change is fine
mkdir -p "$R/lib/modules/7.2.9-fp5/extra"; echo dep > "$R/lib/modules/7.2.9-fp5/modules.dep"
out=$(FP5_KREL=7.2.9-fp5 "$G" check); t "7.2.9-fp5 reported as own release" 'echo "$out" | grep -q "has its own version string"'
t "7.2.9-fp5: kernel pkg change is OK" 'echo "$out" | grep -q "does not affect 7.2.9-fp5"'
out=$(FP5_KREL=7.2.9-nope "$G" check); t "missing modules dir = FAIL" 'echo "$out" | grep -q "^FAIL  no .*7.2.9-nope"'

echo 'deviceinfo_flash_kernel_on_update="true"' > "$R/etc/deviceinfo"
out=$("$G" check); t "flash_kernel_on_update=true = FAIL" 'echo "$out" | grep -q "must be false"'

"$G" pin >/dev/null; t "pin writes =version" 'grep -qx "gnome-settings-daemon=52.0-r0" "$R/etc/apk/world" && grep -qx phosh "$R/etc/apk/world"'
"$G" unpin >/dev/null; t "unpin restores bare name" 'grep -qx gnome-settings-daemon "$R/etc/apk/world" && ! grep -q "gnome-settings-daemon=" "$R/etc/apk/world"'

echo "$pass passed, $failn failed"
[ "$failn" = 0 ]
