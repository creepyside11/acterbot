set -eu
export LC_ALL=C
read_cpu() { awk '/^cpu / {sum=0; for(i=2;i<=9;i++) sum+=$i; printf "%.0f %.0f\n", sum, $5+$6; exit}' /proc/stat; }
set -- $(read_cpu)
total=$1
idle=$2
sleep 1
set -- $(read_cpu)
awk -v dt="$(($1 - total))" -v di="$(($2 - idle))" 'BEGIN {printf "cpu=%.1f\n", (dt>0 ? 100*(dt-di)/dt : 0)}'
awk '
    /^MemTotal:/ {total=$2}
    /^MemAvailable:/ {available=$2}
    END {printf "mem_used=%.0f\nmem_total=%.0f\n", total-available, total}
' /proc/meminfo
LC_ALL=C df -Pk / | awk 'NR == 2 {printf "disk_used=%s\ndisk_total=%s\n", $3, $2}'
awk '{print "load=" $1 " " $2 " " $3}' /proc/loadavg
printf 'cores=%s\n' "$(getconf _NPROCESSORS_ONLN)"
