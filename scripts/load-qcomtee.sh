#!/bin/sh
# Load the out-of-tree qcomtee module, or accept a built-in one.
# The .ko is not in this repo.
# Refuses qsee_fingerpr. No-op if qcomtee is already loaded or built in.
# 7.2.0-nfc-test+ still insmods qcomtee-bridge.ko. A kernel that already
# has /dev/tee0 or the qcomtee driver (7.2.9, CONFIG_QCOMTEE=y,
# CONFIG_QCOM_TZMEM_MODE_GENERIC=y) does not.
# A root caller (the boot unit) runs the commands directly. Everyone
# else still uses sudo -n.
set -e

# qcomtee_plan REL FINGERPR MODULE TEE0 DRIVER CLASS
# Prints refuse-fingerpr, already, builtin, insmod, or refuse-kernel.
qcomtee_plan() {
	rel=$1
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
	# 7.2.0-nfc-test+ has CONFIG_OPTEE, so /sys/class/tee exists with no
	# qcomtee. That kernel still needs the out-of-tree module.
	if [ "$rel" = "7.2.0-nfc-test+" ]; then
		echo insmod
		return 0
	fi
	if [ "$class" = 1 ]; then
		echo builtin
		return 0
	fi
	echo refuse-kernel
	return 0
}

if [ "${1:-}" = "--plan" ]; then
	qcomtee_plan "$2" "$3" "$4" "$5" "$6" "$7"
	exit 0
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

plan=$(qcomtee_plan "$(uname -r)" "$fingerpr" "$mod" "$tee0" "$drv" "$class")
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
	echo "kernel $(uname -r)"
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
run insmod /home/user/fp5-qtee-keep/qcomtee-bridge.ko scm_call_addr=0x$SCM scm_conv_addr=0x$CONV
echo INSMOD_OK
