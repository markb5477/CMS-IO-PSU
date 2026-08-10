#!/usr/bin/env bash
# Turn ON the Prometheus + Grafana stack for the tk-fe-daq DAQ metrics.
# Safe to re-run: it converges the running stack onto the current .env.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { echo "error: no .env here. Run:  cp .env.example .env  and edit it (start with NETWORK BLOCK 1)." >&2; exit 1; }
set -a; . ./.env; set +a

# The rendered config must exist AND be newer than .env, otherwise Prometheus
# silently scrapes yesterday's target - the single most common way this stack
# "breaks" with nothing in the logs.
if [ ! -f prometheus/prometheus.yml ]; then
    echo "prometheus/prometheus.yml missing - rendering it now."
    ./render.sh
elif [ .env -nt prometheus/prometheus.yml ]; then
    echo ".env is newer than prometheus/prometheus.yml - re-rendering so your edits take effect."
    ./render.sh
fi

# .env says yes/no (human); compose wants a compose profile and a Grafana bool.
# The ${arr[@]+"${arr[@]}"} form below is for bash 4.2 (RHEL/CC7), where an empty
# array under `set -u` is an unbound-variable error.
profile_args=()
if [ "${USE_TUNNEL:-no}" = "yes" ]; then
    [ -f tunnel/id_ed25519 ] || {
        echo "error: USE_TUNNEL=yes but tunnel/id_ed25519 is missing. Create and install a passwordless key:" >&2
        echo "         ssh-keygen -t ed25519 -N '' -f tunnel/id_ed25519" >&2
        echo "         ssh-copy-id -i tunnel/id_ed25519.pub ${TUNNEL_SSH_TARGET:-user@daq-pc}" >&2
        exit 1
    }
    chmod 600 tunnel/id_ed25519
    profile_args=(--profile tunnel)
    echo "tunnel: ON  -> ssh ${TUNNEL_SSH_TARGET} then ${TUNNEL_REMOTE_HOSTPORT:-127.0.0.1:9110}, published as daq-tunnel:9110"
else
    echo "tunnel: off -> Prometheus scrapes ${DAQ_EXPORTER_TARGETS} directly"
fi
case "${GRAFANA_ANON:-no}" in yes|true|1) export GRAFANA_ANON_ENABLED=true ;; *) export GRAFANA_ANON_ENABLED=false ;; esac

docker compose ${profile_args[@]+"${profile_args[@]}"} up -d
echo
docker compose ${profile_args[@]+"${profile_args[@]}"} ps
echo

host="$(hostname -f 2>/dev/null || hostname)"
echo "Prometheus : http://localhost:${PROMETHEUS_PORT:-9092}    (LAN: http://${host}:${PROMETHEUS_PORT:-9092})"
echo "Grafana    : http://localhost:${GRAFANA_PORT:-3002}       (LAN: http://${host}:${GRAFANA_PORT:-3002})"
echo "             login ${GRAFANA_ADMIN_USER:-admin} / (GRAFANA_ADMIN_PASSWORD from .env)"
echo "             dashboard: 'TK FE DAQ' folder > 'tk-fe-daq - test beam'"
echo "Retention  : ${PROM_RETENTION:-7d} of history, capped at ${PROM_RETENTION_SIZE:-5GB}"
echo
echo "Now run ./check.sh - it verifies end to end that the DAQ metrics are actually arriving."
