#!/usr/bin/env bash
# Render prometheus/prometheus.yml from the .tmpl using values in .env.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
envsubst < prometheus/prometheus.yml.tmpl > prometheus/prometheus.yml
echo "wrote prometheus/prometheus.yml (scrape target $PSU_EXPORTER_TARGET)"
