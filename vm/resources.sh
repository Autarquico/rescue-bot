#!/usr/bin/env bash
# Instantánea de recursos para el verbo `resources`. Solo lectura.
set -u
export LC_ALL=C

echo "== uptime"
uptime
echo
echo "== memoria (MiB)"
free -m
echo
echo "== disco"
df -h
echo
echo "== top 10 procesos por memoria"
ps -eo pid,user,%mem,%cpu,etime,comm --sort=-%mem | head -n 11
