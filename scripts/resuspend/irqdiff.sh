#!/bin/sh
# irqdiff.sh OLD NEW -- IRQ lines whose summed per-CPU count increased
# (timers/IPIs/crypto engines are noisy and omitted).
awk 'function sum(  i,s){s=0; for(i=2;i<=NF && $i ~ /^[0-9]+$/;i++) s+=$i; return s}
 FNR==NR { if ($1 ~ /:$/) old[$1]=sum(); next }
 $1 ~ /:$/ && $1 !~ /^IPI/ { n=sum(); d=n-old[$1]; nm=$NF;
   if (d>0 && nm !~ /arch_timer|arch_mem_timer|rsc|^ce[0-9]|pmu|mmc|Rescheduling|DP_EXT/) printf "  +%d %s %s %s %s\n", d, $1, $(NF-2), $(NF-1), nm }' "$1" "$2"
