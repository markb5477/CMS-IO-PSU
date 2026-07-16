#!/usr/bin/env bash
# Turn OFF the monitoring stack: Prometheus, Grafana AND the psu-tunnel reverse
# SSH sidecar all come down together (the tunnel's ssh runs inside its container,
# so it dies with it - this also closes the inbound session on the lab PC).
# Data is kept in named volumes; pass --wipe to also delete the Prometheus TSDB
# and Grafana state.
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "--wipe" ]; then
    docker compose down --volumes --remove-orphans
    echo "stopped prometheus + grafana + psu-tunnel and wiped volumes (prometheus-data, grafana-data)."
else
    docker compose down --remove-orphans
    echo "stopped prometheus + grafana + psu-tunnel (volumes kept; ./off.sh --wipe to also delete stored data)."
fi

# sanity: confirm no project container is left running
left="$(docker ps --filter 'name=cpx-' --format '{{.Names}}' || true)"
if [ -n "$left" ]; then
    echo "warning: project containers still up: $left"
else
    echo "confirmed: no cpx-* containers running (reverse SSH tunnel torn down with its container)."
fi
