#!/usr/bin/env bash
# Turn OFF the PSU monitoring (cpx-exporter) on the lab PC.
# The PSU outputs are untouched - stopping the exporter only ends polling/metrics.
#
# Guarantees nothing project-affiliated is left holding the metrics port or the
# PSU socket: stops the systemd service, then reaps any stray exporter started
# outside systemd (e.g. a foreground test run). Experiments are deliberate runs
# and are only flagged, never killed.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${HTTP_PORT:-9820}"

# PIDs currently listening on the metrics port (empty if none / no ss). Reaping
# by PORT rather than by 'exporter.py' is what keeps this from killing the BER
# exporter, which is also named exporter.py but owns a different port (:9821).
pids_on_port() {
    command -v ss >/dev/null 2>&1 || return 0
    ss -ltnpH 2>/dev/null | awk -v p=":${PORT}\$" '$4 ~ p' \
        | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

# 1) the managed user service
if systemctl --user stop cpx-exporter 2>/dev/null; then
    echo "cpx-exporter (user service) stopped."
else
    echo "cpx-exporter user service not running (or not installed)."
fi

# 2) any exporter started outside systemd (foreground test, old shell, etc.)
strays="$(pids_on_port || true)"
if [ -n "$strays" ]; then
    echo "found process(es) still holding :${PORT} outside the service - stopping:"
    ps -o pid,etime,args -p $strays 2>/dev/null || true
    kill $strays 2>/dev/null || true
    sleep 1
    still="$(pids_on_port || true)"
    [ -n "$still" ] && kill -9 $still 2>/dev/null || true
    echo "stray exporter stopped."
fi

# 3) sanity: nothing left listening on the metrics port
if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    echo "warning: something is still listening on :${PORT}:"
    ss -ltnp 2>/dev/null | grep ":${PORT} " || true
else
    echo "confirmed: nothing listening on :${PORT}."
fi

# 4) experiments run the PSU deliberately - never auto-killed, only flagged
exps="$(pgrep -af 'cpx-venv/bin/python' || true)"
if [ -n "$exps" ]; then
    echo
    echo "note: experiment process(es) still running (left alone - they drive the PSU on purpose,"
    echo "      and leave outputs off on exit). Stop yourself if intended:"
    echo "$exps"
fi

echo "done. PSU outputs unchanged; settings persist in the instrument."
