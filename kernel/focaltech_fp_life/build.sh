#!/bin/sh
# Build focaltech_fp_life.ko (0.8-wake) against the phone's kernel tree.
# KDIR must be the tree that built the running kernel (7.2.9, same .config;
# CONFIG_MODVERSIONS is off, so vermagic "7.2.9 SMP preempt mod_unload
# aarch64" is what has to match). Signs with that tree's key if present.
set -eu
KDIR=${KDIR:-/home/kb/src/linux-v7.2.9-sc7280-volte}
D=$(cd "$(dirname "$0")" && pwd)
make -C "$KDIR" M="$D" ARCH=arm64 LLVM=1 modules
if [ -f "$KDIR/certs/signing_key.pem" ]; then
	"$KDIR/scripts/sign-file" sha512 "$KDIR/certs/signing_key.pem" \
		"$KDIR/certs/signing_key.x509" "$D/focaltech_fp_life.ko"
fi
modinfo "$D/focaltech_fp_life.ko" | grep -E '^(version|vermagic|srcversion)'
sha256sum "$D/focaltech_fp_life.ko"
