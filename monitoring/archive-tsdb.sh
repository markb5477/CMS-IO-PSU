#!/usr/bin/env bash
# Archive the Prometheus TSDB volume to ./storage as a .tar.gz WITHOUT deleting
# it. The live data stays in the volume (hot, immediately queryable on restart);
# this is only a belt-and-suspenders backup. Called on every ./on.sh and ./off.sh,
# so overlapping archives may duplicate data - that's intentional and fine.
#
# Names the archive by the data's actual time range when Prometheus is reachable,
# otherwise by wall clock. A short label (e.g. "on"/"off") is appended.
#
# Usage: ./archive-tsdb.sh [label]
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a

label="${1:-manual}"
mkdir -p storage
prom_url="http://localhost:${PROMETHEUS_PORT:-9090}"

# Resolve the actual TSDB volume name (compose prefixes it with the project name).
prom_vol="$(docker volume ls -q --filter name=prometheus-data | head -n1 || true)"
[ -n "$prom_vol" ] || prom_vol="cpx-monitoring_prometheus-data"

# Nothing to archive before the very first start (no volume yet).
if ! docker volume inspect "$prom_vol" >/dev/null 2>&1; then
    echo "archive: no TSDB volume yet ($prom_vol) - nothing to back up."
    exit 0
fi

# Instant-query a Prometheus gauge (empty on any failure / when it's down).
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

# Best-effort naming + BER presence, only meaningful while Prometheus is up.
start="$(fmt "$(q prometheus_tsdb_lowest_timestamp_seconds)")"
end="$(fmt "$(q prometheus_tsdb_head_max_time_seconds)")"
bert_series="$(q 'count(bert_bit_error_rate)')"
if [ -n "$bert_series" ]; then
    echo "archive: BER data present (${bert_series} link series) - included."
else
    echo "archive: Prometheus not queryable for a series count; backing up the on-disk TSDB as-is (PSU + BER if scraped)."
fi

# Quiesce the writer so the tar is consistent. No-op when it isn't running
# (e.g. on turn-on, before 'up -d'); on turn-off this stops it just ahead of the
# caller's 'down'. It does NOT delete anything.
docker compose stop prometheus >/dev/null 2>&1 || true

if [ -n "$start" ] && [ -n "$end" ]; then
    archive="prometheus-tsdb_${start}_${end}_${label}.tar.gz"
else
    archive="prometheus-tsdb_$(date -u +%Y%m%d-%H%M%SZ)_${label}.tar.gz"
fi

docker run --rm \
    -v "${prom_vol}:/data:ro" \
    -v "$(pwd)/storage:/backup" \
    alpine tar czf "/backup/${archive}" -C /data .
echo "archive: TSDB (all series - PSU + BER) -> storage/${archive} ($(du -h "storage/${archive}" | cut -f1))"
