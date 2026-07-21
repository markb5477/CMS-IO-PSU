#!/usr/bin/env bash
# Turn OFF the BER monitoring (bert-exporter) on the LAB PC.
# Stopping the exporter only ends CSV tailing/metrics - the mm_acf DAQ and the
# bertContinuous.csv it writes are untouched.
#
# Stops the systemd service, then reaps anything still holding the metrics port
# (e.g. a foreground test run). Reaping is keyed on the PORT, not the process
# name: the PSU and BER exporters are both called exporter.py, so only the port
# reliably tells them apart - this never touches the PSU exporter.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] && set -a && . ./.env && set +a
PORT="${BERT_HTTP_PORT:-9821}"

# PIDs currently listening on the metrics port (empty if none / no ss).
pids_on_port() {
    command -v ss >/dev/null 2>&1 || return 0
    ss -ltnpH 2>/dev/null | awk -v p=":${PORT}\$" '$4 ~ p' \
        | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

# 1) the managed service, in whichever scope it is installed (see on.sh)
if systemctl cat bert-exporter.service >/dev/null 2>&1; then
    SCOPE=--system
else
    SCOPE=--user
fi
if systemctl "$SCOPE" stop bert-exporter 2>/dev/null; then
    echo "bert-exporter ($SCOPE service) stopped."
else
    echo "bert-exporter $SCOPE service not running, not installed, or needs root."
fi

# 2) anything still bound to the BERT metrics port (foreground test, old shell)
strays="$(pids_on_port || true)"
if [ -n "$strays" ]; then
    echo "found process(es) still holding :${PORT} outside the service - stopping:"
    ps -o pid,etime,args -p $strays 2>/dev/null || true
    kill $strays 2>/dev/null || true
    sleep 1
    still="$(pids_on_port || true)"
    [ -n "$still" ] && kill -9 $still 2>/dev/null || true
    echo "stray bert exporter stopped."
fi

# 3) sanity: nothing left listening on the metrics port
if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    echo "warning: something is still listening on :${PORT}:"
    ss -ltnp 2>/dev/null | grep ":${PORT} " || true
else
    echo "confirmed: nothing listening on :${PORT}."
fi

echo "done. mm_acf DAQ and bertContinuous.csv untouched; only the BER metrics polling stopped."
