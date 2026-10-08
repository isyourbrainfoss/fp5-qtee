#!/bin/sh
# Host test for fetch-focal32-firmware.sh. Uses SYNTHETIC random files named
# focal32.*; no real firmware is involved. Image formats are tested only
# when the tools to build them (mtools, mke2fs, mkfs.erofs, img2simg) exist.
set -eu

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
FETCH=$HERE/fetch-focal32-firmware.sh
T=$(mktemp -d "${TMPDIR:-/tmp}/focal32-test.XXXXXX")
trap 'rm -rf -- "$T"' EXIT
NAMES="mdt b00 b01 b02 b03 b04 b05 b06 b07"
pass=0
fail=0
skip=0

ok() { pass=$((pass + 1)); printf 'ok   %s\n' "$1"; }
# check NAME CMD...: ok when CMD succeeds.
check() {
	name=$1
	shift
	if "$@"; then ok "$name"; else bad "$name"; fi
}
bad() { fail=$((fail + 1)); printf 'FAIL %s\n' "$1"; }
skp() { skip=$((skip + 1)); printf 'skip %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

# expect_rc WANT NAME CMD...: run CMD, compare exit status.
expect_rc() {
	want=$1
	name=$2
	shift 2
	set +e
	"$@" >"$T/out" 2>&1
	rc=$?
	set -e
	if [ "$rc" = "$want" ]; then ok "$name"; else
		bad "$name (rc=$rc, want $want)"
		sed 's/^/     | /' "$T/out"
	fi
}

out_has() {
	if grep -q -- "$2" "$T/out"; then ok "$1"; else
		bad "$1 (no '$2')"
		sed 's/^/     | /' "$T/out"
	fi
}

# Synthetic blobs in a vendor-like tree.
SRC=$T/src/vendor/firmware_mnt/image
mkdir -p "$SRC"
for n in $NAMES; do
	head -c 4096 /dev/urandom >"$SRC/focal32.$n"
done
printf 'not firmware\n' >"$SRC/other.mdt"
cp "$HERE/focal32.sha256" "$T/empty.sha256"
(cd "$SRC" && for n in $NAMES; do sha256sum "focal32.$n"; done) >"$T/good.sha256"

expect_rc 1 "placeholder sums refuse" "$FETCH" --sums "$T/empty.sha256" --dest "$T/d0" "$T/src"
out_has "placeholder explains --record" "no real checksums"
check "nothing installed on refusal" [ ! -e "$T/d0" ]

expect_rc 0 "dry run with good sums" "$FETCH" --sums "$T/good.sha256" --dest "$T/d1" --dry-run "$T/src"
check "dry run writes nothing" [ ! -e "$T/d1" ]

expect_rc 0 "install from directory" "$FETCH" --sums "$T/good.sha256" --dest "$T/d1" "$T/src"
for n in $NAMES; do
	cmp -s "$SRC/focal32.$n" "$T/d1/focal32.$n" || bad "installed focal32.$n differs"
done
ok "installed files match"

head -n 8 "$T/good.sha256" >"$T/short.sha256"
expect_rc 1 "missing entry refuses" "$FETCH" --sums "$T/short.sha256" --dest "$T/d2" "$T/src"
out_has "missing entry named" "focal32.b07"

sed '1s/^./0/;1s/^0\(.\)/f\1/' "$T/good.sha256" >"$T/bad.sha256"
expect_rc 1 "mismatch refuses" "$FETCH" --sums "$T/bad.sha256" --dest "$T/d2" "$T/src"
out_has "mismatch reported" "MISMATCH focal32.mdt"
check "nothing installed on mismatch" [ ! -e "$T/d2" ]

{ cat "$T/good.sha256"; sed -n '1s/^\(.\)/0/p' "$T/good.sha256" | sed '1s/^0/1/'; } >"$T/conf.sha256"
expect_rc 1 "conflicting entries refuse" "$FETCH" --sums "$T/conf.sha256" --dest "$T/d2" "$T/src"
out_has "conflict reported" "conflicting hashes"

cp "$T/empty.sha256" "$T/rec.sha256"
expect_rc 0 "record dry run" "$FETCH" --record --dry-run --sums "$T/rec.sha256" --dest "$T/d3" "$T/src"
check "record dry run leaves sums" cmp -s "$T/empty.sha256" "$T/rec.sha256"
expect_rc 0 "record (trust on first use)" "$FETCH" --record --sums "$T/rec.sha256" --dest "$T/d3" "$T/src"
grep -v '^#' "$T/rec.sha256" | grep -v '^$' | sort >"$T/rec.sorted"
sort "$T/good.sha256" >"$T/good.sorted"
check "recorded hashes are the real ones" cmp -s "$T/rec.sorted" "$T/good.sorted"
expect_rc 1 "second record refuses" "$FETCH" --record --sums "$T/rec.sha256" --dest "$T/d3" "$T/src"
expect_rc 0 "install with recorded sums" "$FETCH" --sums "$T/rec.sha256" --dest "$T/d4" "$T/src"

rm "$SRC/focal32.b05"
expect_rc 1 "incomplete set not found" "$FETCH" --sums "$T/good.sha256" --dest "$T/d5" "$T/src"
head -c 4096 /dev/urandom >"$SRC/focal32.b05"
(cd "$SRC" && for n in $NAMES; do sha256sum "focal32.$n"; done) >"$T/good.sha256"

(cd "$T/src" && tar -czf "$T/fw.tar.gz" vendor)
expect_rc 0 "install from tar.gz" "$FETCH" --sums "$T/good.sha256" --dest "$T/d6" "$T/fw.tar.gz"
if have zip; then
	(cd "$T/src" && zip -qr "$T/fw.zip" vendor)
else
	python3 - "$T/src" "$T/fw.zip" <<'PY'
import os, sys, zipfile
root, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w") as z:
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            z.write(p, os.path.relpath(p, root))
PY
fi
expect_rc 0 "install from zip" "$FETCH" --sums "$T/good.sha256" --dest "$T/d7" "$T/fw.zip"

# FAT modem image (upper-case names, as some FAT tools store them).
if have mformat && have mcopy && have mmd; then
	dd if=/dev/zero of="$T/modem.img" bs=1024 count=2048 2>/dev/null
	mformat -i "$T/modem.img" -F :: 2>/dev/null || mformat -i "$T/modem.img" ::
	mmd -i "$T/modem.img" ::/image
	for n in $NAMES; do
		up=$(printf 'FOCAL32.%s' "$n" | tr '[:lower:]' '[:upper:]')
		mcopy -i "$T/modem.img" "$SRC/focal32.$n" "::/image/$up"
	done
	expect_rc 0 "install from FAT image" "$FETCH" --sums "$T/good.sha256" --dest "$T/d8" "$T/modem.img"
	mkdir -p "$T/pkg"
	cp "$T/modem.img" "$T/pkg/NON-HLOS.bin"
	head -c 70000 /dev/urandom >"$T/pkg/boot.img"
	(cd "$T/pkg" && tar -cf "$T/pkg.tar" .)
	expect_rc 0 "FAT image nested in tar" "$FETCH" --sums "$T/good.sha256" --dest "$T/d9" "$T/pkg.tar"
else
	skp "FAT image (mtools missing)"
fi

if have mke2fs && have debugfs; then
	mke2fs -q -t ext4 -d "$T/src/vendor" "$T/vendor.img" 8M
	expect_rc 0 "install from ext4 image" "$FETCH" --sums "$T/good.sha256" --dest "$T/d10" "$T/vendor.img"
	if have img2simg && have simg2img; then
		img2simg "$T/vendor.img" "$T/vendor.sparse.img"
		expect_rc 0 "install from sparse ext4 image" "$FETCH" --sums "$T/good.sha256" --dest "$T/d11" "$T/vendor.sparse.img"
	else
		skp "sparse image (img2simg/simg2img missing)"
	fi
else
	skp "ext4 image (e2fsprogs missing)"
fi

if have mkfs.erofs && have fsck.erofs; then
	mkfs.erofs "$T/vendor.erofs" "$T/src/vendor" >/dev/null 2>&1
	expect_rc 0 "install from EROFS image" "$FETCH" --sums "$T/good.sha256" --dest "$T/d12" "$T/vendor.erofs"
else
	skp "EROFS image (erofs-utils missing)"
fi

printf 'hello\n' >"$T/junk.bin"
expect_rc 1 "unknown file refused" "$FETCH" --sums "$T/good.sha256" --dest "$T/d13" "$T/junk.bin"
expect_rc 1 "missing source refused" "$FETCH" --sums "$T/good.sha256" "$T/nope"
expect_rc 0 "help" "$FETCH" --help

printf '\n%d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
[ "$fail" = 0 ]
