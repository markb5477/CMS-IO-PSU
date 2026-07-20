#!/usr/bin/env bash
# Turn ON the Prometheus + Grafana stack (on the MONITOR PC). This also pulls the
# BER CSV: the psu-tunnel sidecar forwards the lab PC's bert-exporter (:9821) and
# Prometheus scrapes it via the 'bert_status' job. (The exporter process itself
# runs on the LAB PC - start it there with bert-server/on.sh.)
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a || echo "note: no .env found; using compose defaults (grafana admin/admin, ports 9090/3000)"
[ -f prometheus/prometheus.yml ] || { echo "error: prometheus/prometheus.yml missing - run ./render.sh (or re-deploy)"; exit 1; }

# Make sure the BER scrape path is actually baked into the running config.
if grep -q 'job_name: bert_status' prometheus/prometheus.yml; then
    echo "BER pull: job 'bert_status' -> ${BERT_EXPORTER_TARGET:-psu-tunnel:9821} (tunnel -> ${TUNNEL_BERT_REMOTE_HOSTPORT:-127.0.0.1:9821} on the lab PC)"
else
    echo "warning: 'bert_status' scrape job missing from prometheus.yml - the BER CSV will NOT be pulled."
    echo "         run ./render.sh (needs BERT_EXPORTER_TARGET in .env) and re-run ./on.sh."
fi

# Back up the (at-rest) TSDB before starting, as insurance. Does NOT delete it -
# last session's data stays in the volume and comes up hot. Duplicate/overlapping
# archives with the previous off are expected and fine.
./archive-tsdb.sh on || echo "note: startup TSDB archive skipped/failed; continuing to start the stack."

docker compose up -d
echo
docker compose ps
echo
echo "Prometheus: http://localhost:${PROMETHEUS_PORT:-9090}   Grafana: http://localhost:${GRAFANA_PORT:-3000}"
echo "verify the BER pull: Prometheus > Status > Targets, job 'bert_status' should be UP"
