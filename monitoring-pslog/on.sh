#!/usr/bin/env bash
# Turn ON the Prometheus + Grafana stack for the pscontrol PS log.
#
# The exporter itself is NOT started here: it runs on the pscontrol host as part
# of `ps_run -a monitor`. If the monitor is not running there, this stack comes
# up healthy but the ps_log target is down - that is the PSMonitorNotRunning alert.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a || echo "note: no .env; using compose defaults (grafana admin/admin, ports 9091/3001)"
[ -f prometheus/prometheus.yml ] || { echo "error: prometheus/prometheus.yml missing - run ./render.sh"; exit 1; }

# The tunnel sidecar is opt-in: only start it when we actually scrape through it.
PROFILE=()
case "${PSLOG_EXPORTER_TARGET:-}" in
    pslog-tunnel:*)
        [ -f tunnel/id_ed25519 ] || { echo "error: scraping via pslog-tunnel but tunnel/id_ed25519 is missing"; exit 1; }
        PROFILE=(--profile tunnel) ;;
esac

docker compose "${PROFILE[@]}" up -d
echo
docker compose "${PROFILE[@]}" ps
echo
echo "Prometheus: http://localhost:${PROMETHEUS_PORT:-9091}   Grafana: http://localhost:${GRAFANA_PORT:-3001}"
echo "check the pull: Prometheus > Status > Targets, job 'ps_log' should be UP"
