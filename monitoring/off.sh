#!/usr/bin/env bash
# Turn OFF the monitoring stack: Prometheus, Grafana AND the psu-tunnel reverse
# SSH sidecar all come down together (the tunnel's ssh runs inside its container,
# so it dies with it - this also closes the inbound session on the lab PC).
#
# Data is kept in named volumes. Pass --wipe to reset the volumes for a fresh
# start; before wiping, the Prometheus TSDB is archived to ./storage as
# prometheus-tsdb_<start>_<end>.tar.gz (start/end = the actual data time range),
# so the recorded data is preserved rather than lost.
set -euo pipefail
cd "$(dirname "$0")"

# Load PROMETHEUS_PORT (and friends) if present, so we query the right port.
[ -f .env ] && set -a && . ./.env && set +a

if [ "${1:-}" != "--wipe" ]; then
    docker compose down --remove-orphans
    echo "stopped prometheus + grafana + psu-tunnel (volumes kept; ./off.sh --wipe to archive+reset stored data)."
else
    # --- archive the Prometheus TSDB before wiping -------------------------
    mkdir -p storage
    prom_url="http://localhost:${PROMETHEUS_PORT:-9090}"

    # Resolve the actual TSDB volume name (compose prefixes with the project name).
    prom_vol="$(docker volume ls -q --filter name=prometheus-data | head -n1 || true)"
    [ -n "$prom_vol" ] || prom_vol="cpx-monitoring_prometheus-data"

    # Instant-query a Prometheus gauge, returning its numeric value (empty on failure).
    q() {
        curl -sf --max-time 5 "$prom_url/api/v1/query" \
            --data-urlencode "query=$1" 2>/dev/null \
            | jq -r '.data.result[0].value[1] // empty' 2>/dev/null || true
    }
    # Epoch -> UTC stamp; empty if the value isn't a sane timestamp.
    fmt() {
        local i; i="$(printf '%.0f' "$1" 2>/dev/null || true)"
        [ -n "$i" ] && [ "$i" -gt 0 ] 2>/dev/null && date -u -d "@$i" +%Y%m%d-%H%M%SZ || true
    }

    start="$(fmt "$(q prometheus_tsdb_lowest_timestamp_seconds)")"
    end="$(fmt "$(q prometheus_tsdb_head_max_time_seconds)")"
    if [ -n "$start" ] && [ -n "$end" ]; then
        archive="prometheus-tsdb_${start}_${end}.tar.gz"
    else
        # Prometheus unreachable / empty - still archive, name it by wall clock.
        archive="prometheus-tsdb_$(date -u +%Y%m%d-%H%M%SZ).tar.gz"
        echo "note: could not read data time range from Prometheus; naming archive by current time."
    fi

    # Stop the writer so the on-disk TSDB is consistent, then tar its raw volume.
    docker compose stop prometheus
    docker run --rm \
        -v "${prom_vol}:/data:ro" \
        -v "$(pwd)/storage:/backup" \
        alpine tar czf "/backup/${archive}" -C /data .
    echo "archived Prometheus TSDB -> storage/${archive} ($(du -h "storage/${archive}" | cut -f1))"

    # --- now tear everything down and reset the volumes --------------------
    docker compose down --volumes --remove-orphans
    echo "stopped prometheus + grafana + psu-tunnel and reset volumes (TSDB kept in storage/${archive})."
fi

# sanity: confirm no project container is left running
left="$(docker ps --filter 'name=cpx-' --format '{{.Names}}' || true)"
if [ -n "$left" ]; then
    echo "warning: project containers still up: $left"
else
    echo "confirmed: no cpx-* containers running (reverse SSH tunnel torn down with its container)."
fi
