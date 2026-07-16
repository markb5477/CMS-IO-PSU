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

# 1) the managed user service
if systemctl --user stop cpx-exporter 2>/dev/null; then
    echo "cpx-exporter (user service) stopped."
else
    echo "cpx-exporter user service not running (or not installed)."
fi

# 2) any exporter.py started outside systemd (foreground test, old shell, etc.)
strays="$(pgrep -f 'exporter\.py' || true)"
if [ -n "$strays" ]; then
    echo "found exporter.py running outside the service - stopping it:"
    ps -o pid,etime,args -p $strays || true
    kill $strays 2>/dev/null || true
    sleep 1
    still="$(pgrep -f 'exporter\.py' || true)"
    [ -n "$still" ] && kill -9 $still 2>/dev/null || true
    echo "stray exporter.py stopped."
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
