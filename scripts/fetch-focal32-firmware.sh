#!/bin/sh
# Copy the signed focal32 trustlet (focal32.mdt + focal32.b00..b07) from a
# Fairphone 5 stock image into the directory fp5-qtee-session loads from,
# after checking every file against scripts/focal32.sha256.
#
# The blobs are proprietary and signed by Qualcomm/the OEM. Never commit them.
set -eu

PROG=${0##*/}
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
SUMS=$HERE/focal32.sha256
DEST=/lib/firmware/qsee
DRY=0
RECORD=0
SRC=
FILES="focal32.mdt focal32.b00 focal32.b01 focal32.b02 focal32.b03 focal32.b04 focal32.b05 focal32.b06 focal32.b07"
MAX_BYTES=8388608 # fp5-qtee-session refuses larger files
MAX_DEPTH=4
WORK=

usage() {
	cat <<USAGE
Usage: $PROG [--dest DIR] [--sums FILE] [--dry-run] [--record] SOURCE

Find focal32.mdt and focal32.b00..focal32.b07 in SOURCE, check their
SHA256 against the checksum file, and copy them to DIR.

SOURCE can be:
  - a directory (an extracted image or firmware tree),
  - a .zip or .tar[.gz|.xz|.bz2|.zst] archive, such as the Fairphone 5
    factory package (FP5-*-factory.zip),
  - a filesystem image: FAT (modem / NON-HLOS, needs mtools or 7z),
    ext4 (needs debugfs from e2fsprogs), EROFS (needs fsck.erofs from
    erofs-utils), Android sparse (needs simg2img), or a super image (needs
    lpunpack; only vendor* and odm* partitions are opened).
Archives and images found inside SOURCE are opened too, up to $MAX_DEPTH
levels deep, looking at modem/NON-HLOS/vendor/odm/super/firmware images first.
On Qualcomm vendor images the trustlets usually sit in
/vendor/firmware_mnt/image (the modem partition's image/ directory) or in
/vendor/firmware.

Options:
  --dest DIR    install directory (default /lib/firmware/qsee, the session's
                default and the Finger app's FIRMWARE)
  --sums FILE   checksum file (default: focal32.sha256 next to this script)
  --dry-run     find and check the files, print what would happen, change
                nothing
  --record      trust on first use: write the SHA256 of the files found in
                SOURCE into the checksum file, then install them. Only
                allowed while the checksum file has no real entries. Use it
                once, with a copy you trust (for example the files from a
                phone where focal32 already loads and enrolls).
  -h, --help    this text

Checksum file: one "<sha256>  focal32.xxx" line per file, sha256sum
format. Lines starting with # are ignored. The script refuses to install
when any of the nine entries is missing or any hash differs.

Where to get a stock image: Fairphone publishes FP5 factory packages at
https://support.fairphone.com/hc/en-us/articles/18896094650513
The postmarketOS package firmware-fairphone-fp5 (from
https://github.com/FairBlobs/FP5-firmware) does not include focal32.
USAGE
}

die() {
	printf '%s: %s\n' "$PROG" "$*" >&2
	exit 1
}

say() {
	printf '%s\n' "$*"
}

cleanup() {
	if [ -n "$WORK" ] && [ -d "$WORK" ]; then
		rm -rf -- "$WORK"
	fi
}

have() {
	command -v "$1" >/dev/null 2>&1
}

sha256_of() {
	if have sha256sum; then
		sha256sum -- "$1" | cut -d' ' -f1
	elif have shasum; then
		shasum -a 256 -- "$1" | cut -d' ' -f1
	elif have openssl; then
		openssl dgst -sha256 -r -- "$1" | cut -d' ' -f1
	else
		die "need sha256sum, shasum or openssl"
	fi
}

# Hex bytes of FILE at OFFSET, LEN long, no spaces.
magic() {
	od -An -tx1 -j "$2" -N "$3" -- "$1" 2>/dev/null | tr -d ' \n'
}

detect() {
	f=$1
	case $(magic "$f" 0 4) in
	504b0304) say zip; return ;;
	3aff26ed) say sparse; return ;;
	1f8b*) say tar; return ;;
	fd377a58) say tar; return ;;
	28b52ffd) say tar; return ;;
	425a68*) say tar; return ;;
	esac
	if [ "$(magic "$f" 257 5)" = 7573746172 ]; then
		say tar
		return
	fi
	if [ "$(magic "$f" 1024 4)" = e2e1f5e0 ]; then
		say erofs
		return
	fi
	if [ "$(magic "$f" 1080 2)" = 53ef ]; then
		say ext4
		return
	fi
	if [ "$(magic "$f" 4096 4)" = 67446c61 ]; then
		say super
		return
	fi
	if [ "$(magic "$f" 510 2)" = 55aa ]; then
		case "$(magic "$f" 54 3)$(magic "$f" 82 3)" in
		*464154*) say fat; return ;;
		esac
	fi
	say unknown
}

# unpack FILE TYPE OUTDIR. Returns 1 when FILE is not something we open.
# POSIX sh has no local variables, hence the u_ prefix.
unpack() {
	u_f=$1
	u_t=$2
	u_o=$3
	mkdir -p -- "$u_o"
	case $u_t in
	zip)
		if have unzip; then
			unzip -q -o -- "$u_f" -d "$u_o" || die "unzip failed on $u_f"
		elif have 7z; then
			7z x -y -o"$u_o" -- "$u_f" >/dev/null || die "7z failed on $u_f"
		elif have bsdtar; then
			bsdtar -xf "$u_f" -C "$u_o" || die "bsdtar failed on $u_f"
		elif have python3; then
			python3 -m zipfile -e "$u_f" "$u_o" || die "python3 zipfile failed on $u_f"
		else
			die "$u_f is a zip; install unzip, 7z, bsdtar or python3"
		fi
		;;
	tar)
		tar -xf "$u_f" -C "$u_o" ||
			die "tar could not extract $u_f (is the compressor installed?)"
		;;
	sparse)
		have simg2img ||
			die "$u_f is an Android sparse image; install simg2img (android-tools / android-sdk-libsparse-utils)"
		simg2img "$u_f" "$u_o/raw.img" || die "simg2img failed on $u_f"
		;;
	super)
		have lpunpack ||
			die "$u_f is a super image; install lpunpack (android-tools), or extract vendor.img yourself"
		mkdir -p -- "$u_o/parts"
		for p in vendor_a vendor odm_a odm; do
			lpunpack -p "$p" "$u_f" "$u_o/parts" >/dev/null 2>&1 || :
		done
		;;
	ext4)
		have debugfs || die "$u_f is ext4; install e2fsprogs (debugfs)"
		# Known trustlet directories first, then the whole image.
		for d in /firmware_mnt/image /firmware /etc/firmware \
			/vendor/firmware_mnt/image /vendor/firmware; do
			debugfs -R "rdump $d $u_o" "$u_f" >/dev/null 2>&1 || :
		done
		if ! find "$u_o" -type f -iname focal32.mdt | grep -q .; then
			debugfs -R "rdump / $u_o" "$u_f" >/dev/null 2>&1 ||
				die "debugfs could not read $u_f"
		fi
		;;
	erofs)
		if have fsck.erofs; then
			fsck.erofs --extract="$u_o/root" "$u_f" >/dev/null ||
				die "fsck.erofs --extract failed on $u_f (needs erofs-utils 1.5 or later)"
		elif have 7z; then
			7z x -y -o"$u_o" -- "$u_f" >/dev/null || die "7z failed on $u_f"
		else
			die "$u_f is EROFS; install erofs-utils (fsck.erofs)"
		fi
		;;
	fat)
		if have mcopy; then
			mcopy -s -n -i "$u_f" ::/ "$u_o/" 2>/dev/null ||
				die "mcopy failed on $u_f"
		elif have 7z; then
			7z x -y -o"$u_o" -- "$u_f" >/dev/null || die "7z failed on $u_f"
		else
			die "$u_f is a FAT image; install mtools (mcopy) or 7z"
		fi
		;;
	*)
		return 1
		;;
	esac
	return 0
}

# Print the directory holding focal32.mdt and all of b00..b07, or nothing.
find_set() {
	find "$1" -type f -iname focal32.mdt 2>/dev/null | while IFS= read -r m; do
		d=$(dirname -- "$m")
		ok=1
		for n in $FILES; do
			if [ -z "$(find "$d" -maxdepth 1 -type f -iname "$n" | head -n 1)" ]; then
				ok=0
				break
			fi
		done
		if [ "$ok" = 1 ]; then
			say "$d"
			break
		fi
	done
}

# search DIR DEPTH: writes the found directory to $WORK/found.
# Recursion runs in a subshell so the caller's variables survive.
search() {
	s_dir=$1
	s_depth=$2
	s_hit=$(find_set "$s_dir")
	if [ -n "$s_hit" ]; then
		say "$s_hit" >"$WORK/found"
		return 0
	fi
	[ "$s_depth" -lt "$MAX_DEPTH" ] || return 1
	s_list=$WORK/list.$s_depth
	find "$s_dir" -type f \( -iname '*.img' -o -iname '*.bin' -o -iname '*.zip' \
		-o -iname '*.tar' -o -iname '*.tar.*' -o -iname '*.tgz' \) 2>/dev/null |
		awk '{ l = tolower($0); sub(/.*\//, "", l);
			p = (l ~ /^(modem|non-hlos|vendor|odm|super|firmware)/) ? 0 : 1;
			print p "\t" $0 }' | sort | cut -f2- >"$s_list"
	s_n=0
	while IFS= read -r s_f; do
		s_t=$(detect "$s_f")
		[ "$s_t" = unknown ] && continue
		s_n=$((s_n + 1))
		s_out=$s_dir.d$s_depth.$s_n
		case $s_dir in "$WORK"/*) ;; *) s_out=$WORK/d$s_depth.$s_n ;; esac
		say "opening $s_f ($s_t)"
		(unpack "$s_f" "$s_t" "$s_out") || continue
		if (search "$s_out" $((s_depth + 1))); then
			return 0
		fi
		# Keep disk use down: drop what did not contain the files.
		rm -rf -- "$s_out"
	done <"$s_list"
	return 1
}

# Real entries in the checksum file: "<64 hex>  focal32.xxx".
real_entries() {
	[ -f "$SUMS" ] || return 0
	grep -E '^[0-9a-fA-F]{64}[[:space:]]+\*?focal32\.(mdt|b0[0-7])[[:space:]]*$' "$SUMS" || :
}

expected() {
	real_entries | awk -v n="$1" '{ f = $2; sub(/^\*/, "", f);
		if (f == n) print tolower($1) }' | sort -u
}

while [ $# -gt 0 ]; do
	case $1 in
	--dest)
		[ $# -ge 2 ] || die "--dest needs a directory"
		DEST=$2
		shift 2
		;;
	--dest=*)
		DEST=${1#--dest=}
		shift
		;;
	--sums)
		[ $# -ge 2 ] || die "--sums needs a file"
		SUMS=$2
		shift 2
		;;
	--sums=*)
		SUMS=${1#--sums=}
		shift
		;;
	--dry-run)
		DRY=1
		shift
		;;
	--record)
		RECORD=1
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	--)
		shift
		break
		;;
	-*)
		die "unknown option $1 (see --help)"
		;;
	*)
		[ -z "$SRC" ] || die "only one SOURCE, got '$SRC' and '$1'"
		SRC=$1
		shift
		;;
	esac
done
if [ $# -gt 0 ]; then
	[ -z "$SRC" ] && [ $# -eq 1 ] || die "only one SOURCE"
	SRC=$1
fi
[ -n "$SRC" ] || {
	usage >&2
	exit 2
}
[ -e "$SRC" ] || die "no such file or directory: $SRC"
[ -n "$DEST" ] || die "--dest is empty"

# Decide on the checksum policy before doing any work.
nreal=$(real_entries | wc -l | tr -d ' ')
if [ "$RECORD" = 1 ]; then
	[ "$nreal" = 0 ] ||
		die "$SUMS already has $nreal real entries; --record only works on an empty file. Edit it by hand to re-record."
else
	if [ "$nreal" = 0 ]; then
		die "$SUMS has no real checksums yet. Refusing to install unverified blobs.
Fill it in from a known-good copy, or run once with --record on a source you
trust (trust on first use) and commit the resulting file."
	fi
	missing=
	for n in $FILES; do
		c=$(expected "$n" | wc -l | tr -d ' ')
		[ "$c" -le 1 ] || die "$SUMS has conflicting hashes for $n"
		[ "$c" = 1 ] || missing="$missing $n"
	done
	[ -z "$missing" ] || die "$SUMS has no checksum for:$missing"
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/focal32-fetch.XXXXXX")
trap cleanup EXIT
trap 'exit 130' INT TERM

if [ -d "$SRC" ]; then
	search "$SRC" 0 || :
else
	t=$(detect "$SRC")
	[ "$t" != unknown ] ||
		die "$SRC is not a directory, archive or filesystem image this script knows"
	say "opening $SRC ($t)"
	unpack "$SRC" "$t" "$WORK/src" || die "could not open $SRC"
	search "$WORK/src" 1 || :
fi
[ -s "$WORK/found" ] ||
	die "focal32.mdt with focal32.b00..b07 not found in $SRC"
FOUND=$(cat "$WORK/found")
say "found focal32 in $FOUND"

# Resolve each file (names may be upper case on FAT) and check it.
fail=0
for n in $FILES; do
	f=$(find "$FOUND" -maxdepth 1 -type f -iname "$n" | head -n 1)
	sz=$(wc -c <"$f" | tr -d ' ')
	if [ "$sz" -le 0 ] || [ "$sz" -gt "$MAX_BYTES" ]; then
		say "BAD SIZE $n: $sz bytes (session accepts 1..$MAX_BYTES)"
		fail=1
		continue
	fi
	h=$(sha256_of "$f")
	printf '%s\n' "$h  $n" >>"$WORK/actual"
	if [ "$RECORD" = 0 ]; then
		e=$(expected "$n")
		if [ "$h" = "$e" ]; then
			say "ok       $n  $h"
		else
			say "MISMATCH $n  got $h"
			say "                 want $e"
			fail=1
		fi
	else
		say "record   $n  $h"
	fi
done
[ "$fail" = 0 ] || die "refusing to install: checksum or size check failed"

if [ "$RECORD" = 1 ]; then
	if [ "$DRY" = 1 ]; then
		say "dry run: would write these entries to $SUMS"
	else
		tmp=$SUMS.tmp.$$
		{
			if [ -f "$SUMS" ]; then
				grep -E '^[[:space:]]*(#|$)' "$SUMS" || :
			fi
			printf '# Recorded %s by %s --record from:\n#   %s\n' \
				"$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PROG" "$SRC"
			cat "$WORK/actual"
		} >"$tmp"
		mv -f -- "$tmp" "$SUMS"
		say "wrote $SUMS. Review it and commit it."
	fi
fi

if [ "$DRY" = 1 ]; then
	for n in $FILES; do
		say "dry run: would install $DEST/$n"
	done
	exit 0
fi

mkdir -p -- "$DEST" 2>/dev/null || die "cannot create $DEST (try sudo)"
[ -w "$DEST" ] || die "cannot write to $DEST (try sudo)"
for n in $FILES; do
	f=$(find "$FOUND" -maxdepth 1 -type f -iname "$n" | head -n 1)
	want=$(awk -v n="$n" '$2 == n { print $1 }' "$WORK/actual")
	tmp=$DEST/.$n.tmp.$$
	cp -- "$f" "$tmp" || die "copy to $tmp failed"
	chmod 0644 "$tmp"
	if [ "$(sha256_of "$tmp")" != "$want" ]; then
		rm -f -- "$tmp"
		die "copy of $n does not match after writing; nothing more installed"
	fi
	mv -f -- "$tmp" "$DEST/$n"
	say "installed $DEST/$n"
done
say "done. fp5-qtee-session loads from $DEST (its second argument)."
