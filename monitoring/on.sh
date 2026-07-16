#!/usr/bin/env bash
# Turn ON the Prometheus + Grafana stack (on the monitoring host).
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || echo "note: no .env found; using compose defaults (grafana admin/admin, ports 9090/3000)"
[ -f prometheus/prometheus.yml ] || { echo "error: prometheus/prometheus.yml missing - run ./render.sh (or re-deploy)"; exit 1; }
docker compose up -d
echo
docker compose ps
echo
echo "Prometheus: http://localhost:${PROMETHEUS_PORT:-9090}   Grafana: http://localhost:${GRAFANA_PORT:-3000}"
