#!/bin/sh
# Load the out-of-tree qcomtee module. The .ko is not in this repo.
# Refuses qsee_fingerpr. No-op if qcomtee is already loaded.
set -e
rel=$(uname -r)
if [ "$rel" != "7.2.0-nfc-test+" ]; then
  echo "kernel $rel"
  exit 1
fi
if lsmod | awk "\$1==\"qsee_fingerpr\" {f=1} END{exit !f}"; then
  echo "qsee_fingerpr is loaded"
  exit 1
fi
if lsmod | awk "\$1==\"qcomtee\" {f=1} END{exit !f}"; then
  echo "qcomtee already loaded"
  exit 0
fi
SCM=$(sudo -n awk "\$3==\"__scm_smc_call\" {print \$1}" /proc/kallsyms)
CONV=$(sudo -n awk "\$3==\"qcom_scm_convention\" {print \$1}" /proc/kallsyms)
sudo -n insmod /home/user/fp5-qtee-keep/qcomtee-bridge.ko scm_call_addr=0x$SCM scm_conv_addr=0x$CONV
echo INSMOD_OK
