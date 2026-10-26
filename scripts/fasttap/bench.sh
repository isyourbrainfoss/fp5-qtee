#!/bin/sh
# A/B bench, no finger: idle captures with the watcher stopped.
# Usage: bench.sh <binary> <label> [VAR=value ...]
# Writes $K/bench/<label>.txt. Restarts the watcher after.
set -eu
D=$(cd "$(dirname "$0")" && pwd)
. "$D/common.sh"
bin=$1; label=$2; shift 2
mkdir -p "$K/bench"
take_lock
stop_all
out="$K/bench/$label.txt"
say "bench $label: $bin $*"
sudo -n env "$@" timeout -k 5 120 "$bin" bench /lib/firmware/qsee > "$out" 2>&1 || say "bench $label rc=$?"
flock -u 9
start_unit
grep -E "^TIME .*bench|BOOST|diag " "$out" | tail -n 40
