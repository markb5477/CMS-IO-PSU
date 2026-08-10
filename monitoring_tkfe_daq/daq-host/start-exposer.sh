#!/usr/bin/env bash
##############################################################################
# RUN THIS ON THE *DAQ PC*, not on the monitoring PC.
#
# Copy this one file over (it needs nothing from the rest of the folder):
#   scp daq-host/start-exposer.sh user@daq-pc:~/
#
# It starts scripts/prometheus_exposer.py from the tk-fe-daq checkout and serves
# the DAQ's .prom file at http://<this-pc>:9110/metrics for Prometheus to scrape.
#
#   ./start-exposer.sh                     serve /var/tmp/tkfe/metrics.prom on :9110
#   ./start-exposer.sh -f /path/x.prom     serve another file
#   ./start-exposer.sh -p 9111             another port
#   ./start-exposer.sh --install-service   install+start a systemd --user service
#                                          so it survives logout and reboots
#
# PAIR IT WITH THE DAQ: point the DAQ at the SAME fixed path, so the exposer
# does not have to be restarted for every run (each run otherwise gets its own
# run-<timestamp> directory, and the exposer would be left serving the old one):
#
#   ext_trigger_daq_double_readout_v2 ... --metrics-file /metrics/tkfe_daq_it.prom
#
# THE DAQ USUALLY RUNS IN THE BOARD CONTAINER, so that path must exist on both
# sides. Per tk-fe-daq/metrics_exposer.md the convention is a world-writable host
# directory bind-mounted in:
#   sudo mkdir -p /metrics && sudo chmod 777 /metrics
#   docker run ... -v /metrics:/metrics     # podman needs -v /metrics:/metrics:z
#                                           # (without :z SELinux blocks it silently)
# This script runs on the HOST and reads the host side of that mount.
#
# The DAQ writes to a .tmp and renames, so a scrape never sees half a file.
##############################################################################
set -euo pipefail

METRICS_FILE="${TKFE_METRICS_FILE:-/metrics/tkfe_daq_it.prom}"
PORT="${TKFE_EXPOSER_PORT:-9110}"
# 0.0.0.0 so a Prometheus on another host can reach it. metrics_exposer.md warns
# that :9110 is unauthenticated - on an untrusted network prefer 127.0.0.1 here
# plus USE_TUNNEL=yes on the monitoring side: the ssh forward lands on
# 127.0.0.1:9110, so nothing is exposed off-box at all.
BIND="${TKFE_EXPOSER_BIND:-0.0.0.0}"
REPO="${TKFE_REPO:-}"
INSTALL_SERVICE=0

while [ $# -gt 0 ]; do
    case "$1" in
        -f|--metrics-file) METRICS_FILE="$2"; shift 2 ;;
        -p|--port)         PORT="$2"; shift 2 ;;
        -b|--bind)         BIND="$2"; shift 2 ;;
        -r|--repo)         REPO="$2"; shift 2 ;;
        --install-service) INSTALL_SERVICE=1; shift ;;
        -h|--help)         sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
done

# Locate scripts/prometheus_exposer.py: explicit --repo, then the usual checkouts.
find_exposer() {
    local c
    for c in \
        "${REPO:+$REPO/scripts/prometheus_exposer.py}" \
        "$(dirname "$0")/prometheus_exposer.py" \
        "$HOME/tk-fe-daq/scripts/prometheus_exposer.py" \
        "$HOME/testbeamv2/tk-fe-daq/scripts/prometheus_exposer.py" \
        "$HOME/beamtest/testbeamv2/tk-fe-daq/scripts/prometheus_exposer.py" \
        "/opt/tk-fe-daq/scripts/prometheus_exposer.py"
    do
        [ -n "$c" ] && [ -f "$c" ] && { echo "$c"; return 0; }
    done
    return 1
}
EXPOSER="$(find_exposer)" || {
    echo "error: could not find scripts/prometheus_exposer.py" >&2
    echo "       pass the checkout explicitly:  $0 --repo /path/to/tk-fe-daq" >&2
    exit 1
}

command -v python3 >/dev/null || { echo "error: python3 not found (the exposer is stdlib-only, but it does need python3)" >&2; exit 1; }

# World-writable, because the DAQ writes this file from inside the board
# container as a different uid. Matches metrics_exposer.md's cold-start step A.
metrics_dir="$(dirname "$METRICS_FILE")"
if [ ! -d "$metrics_dir" ]; then
    mkdir -p "$metrics_dir" 2>/dev/null || { echo "note: need root to create $metrics_dir"; sudo mkdir -p "$metrics_dir"; }
    chmod 777 "$metrics_dir" 2>/dev/null || sudo chmod 777 "$metrics_dir" 2>/dev/null || \
        echo "warning: could not chmod 777 $metrics_dir - a containerised DAQ may not be able to write into it"
fi

if [ "$INSTALL_SERVICE" -eq 1 ]; then
    unit="$HOME/.config/systemd/user/tkfe-exposer.service"
    mkdir -p "$(dirname "$unit")"
    cat > "$unit" <<EOF
[Unit]
Description=tk-fe-daq Prometheus exposer (serves ${METRICS_FILE} on :${PORT})
After=network-online.target

[Service]
ExecStart=$(command -v python3) ${EXPOSER} ${METRICS_FILE} --port ${PORT} --bind ${BIND}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now tkfe-exposer.service
    # without lingering the service dies at logout, which is exactly when nobody notices
    loginctl enable-linger "$USER" 2>/dev/null || echo "note: could not enable linger; the service stops when you log out"
    echo "installed and started: systemctl --user status tkfe-exposer"
    echo "logs:                  journalctl --user -u tkfe-exposer -f"
else
    echo "exposer : $EXPOSER"
    echo "serving : $METRICS_FILE"
    echo "at      : http://$(hostname -f 2>/dev/null || hostname):${PORT}/metrics   (bind ${BIND})"
    echo
    echo "Point the DAQ at the same file:  --metrics-file ${METRICS_FILE}"
    echo "Containerised DAQ? mount it in:  -v ${metrics_dir}:${metrics_dir}   (podman: add :z)"
    echo "If the monitoring PC cannot reach it, the port is probably closed:"
    echo "  sudo firewall-cmd --add-port=${PORT}/tcp        # RHEL/Alma, add --permanent to persist"
    echo "  sudo ufw allow ${PORT}/tcp                      # Ubuntu/Debian"
    echo
    exec python3 "$EXPOSER" "$METRICS_FILE" --port "$PORT" --bind "$BIND"
fi
