#!/bin/sh
# Make sure the fingerprint reader has a QTEE device (/dev/tee0).
# Decides by what the kernel offers, never by the kernel version:
#  - qsee_fingerpr loaded            -> refuse (never load both)
#  - qcomtee module already loaded   -> nothing to do
#  - /dev/tee0 or the qcomtee driver -> built in (CONFIG_QCOMTEE=y), nothing to do
#  - else, if the out-of-tree bridge module exists ($QCOMTEE_BRIDGE_KO,
#    default /home/user/fp5-qtee-keep/qcomtee-bridge.ko) -> insmod it
#  - else refuse.
# BengalOS test kernel: CONFIG_QCOMTEE=y, so this only ever says "built-in".
# A root caller (the boot unit) runs the commands directly. Everyone
# else still uses sudo -n (Nura only; BengalOS has no sudo).
set -e

# qcomtee_plan BRIDGE_KO FINGERPR MODULE TEE0 DRIVER CLASS
# BRIDGE_KO is 1 when the out-of-tree bridge module file exists.
# CLASS (/sys/class/tee) alone is not enough: OPTEE also creates it.
# Prints refuse-fingerpr, already, builtin, insmod, or refuse-kernel.
qcomtee_plan() {
	ko=$1
	fingerpr=$2
	mod=$3
	tee0=$4
	drv=$5
	class=$6
	if [ "$fingerpr" = 1 ]; then
		echo refuse-fingerpr
		return 0
	fi
	if [ "$mod" = 1 ]; then
		echo already
		return 0
	fi
	# /dev/tee0 or the qcomtee driver means the reader is registered.
	if [ "$tee0" = 1 ] || [ "$drv" = 1 ]; then
		echo builtin
		return 0
	fi
	if [ "$ko" = 1 ]; then
		echo insmod
		return 0
	fi
	: "$class"
	echo refuse-kernel
	return 0
}

if [ "${1:-}" = "--plan" ]; then
	qcomtee_plan "$2" "$3" "$4" "$5" "$6" "$7"
	exit 0
fi

BRIDGE_KO=${QCOMTEE_BRIDGE_KO:-/home/user/fp5-qtee-keep/qcomtee-bridge.ko}
ko=0
if [ -f "$BRIDGE_KO" ]; then
	ko=1
fi
fingerpr=0
mod=0
tee0=0
drv=0
class=0
if lsmod | awk "\$1==\"qsee_fingerpr\" {f=1} END{exit !f}"; then
	fingerpr=1
fi
if lsmod | awk "\$1==\"qcomtee\" {f=1} END{exit !f}"; then
	mod=1
fi
if [ -e /dev/tee0 ]; then
	tee0=1
fi
if [ -d /sys/bus/platform/drivers/qcomtee ]; then
	drv=1
fi
if [ -d /sys/class/tee ]; then
	class=1
fi

plan=$(qcomtee_plan "$ko" "$fingerpr" "$mod" "$tee0" "$drv" "$class")
case "$plan" in
refuse-fingerpr)
	echo "qsee_fingerpr is loaded"
	exit 1
	;;
already)
	echo "qcomtee already loaded"
	exit 0
	;;
builtin)
	echo "qcomtee built-in"
	exit 0
	;;
refuse-kernel)
	echo "kernel has no qcomtee (no /dev/tee0, no qcomtee driver) and no $BRIDGE_KO"
	exit 1
	;;
insmod)
	;;
*)
	echo "qcomtee plan $plan"
	exit 1
	;;
esac

if [ "$(id -u)" -eq 0 ]; then
	run() { "$@"; }
else
	run() { sudo -n "$@"; }
fi
SCM=$(run awk "\$3==\"__scm_smc_call\" {print \$1}" /proc/kallsyms)
CONV=$(run awk "\$3==\"qcom_scm_convention\" {print \$1}" /proc/kallsyms)
run insmod "$BRIDGE_KO" scm_call_addr=0x$SCM scm_conv_addr=0x$CONV
echo INSMOD_OK
