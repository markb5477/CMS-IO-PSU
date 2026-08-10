#!/usr/bin/env bash
# Apply an edited scrape config to a RUNNING Prometheus, without a restart and
# without touching the TSDB. Use this when the DAQ PC's address changes mid-run
# (new lease, moved to another subnet, second DAQ PC added):
#
#   edit .env   ->   ./reload.sh
#
# Port/password/retention changes are NOT config-file settings - those need
# ./on.sh (which recreates the containers; the data volume survives).
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a

./render.sh
url="http://localhost:${PROMETHEUS_PORT:-9092}"

if ! curl -sf --max-time 5 -X POST "$url/-/reload" >/dev/null; then
    echo "could not POST $url/-/reload - is the stack up? falling back to a container restart."
    docker compose up -d --force-recreate prometheus
else
    echo "Prometheus reloaded its config in place (TSDB untouched)."
fi
echo "check it took: $url/targets"
