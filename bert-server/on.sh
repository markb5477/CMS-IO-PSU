#!/usr/bin/env bash
# Turn ON the BER monitoring (bert-exporter) on the LAB PC.
# This is what actually watches bertContinuous.csv and serves it on :9821 for the
# monitor PC to scrape. Uses the per-user systemd service (no sudo). See
# bert-server/README.md for the one-time install of
# ~/.config/systemd/user/bert-exporter.service.
set -euo pipefail
cd "$(dirname "$0")"

# Load BERT_HTTP_PORT (and friends) if a local .env is present, so we print the
# right port; the service reads the same .env itself.
[ -f .env ] && set -a && . ./.env && set +a

systemctl --user start bert-exporter
systemctl --user --no-pager status bert-exporter | head -5
echo
echo "metrics: curl -s localhost:${BERT_HTTP_PORT:-9821}/metrics | head"
echo "status : curl -s localhost:${BERT_HTTP_PORT:-9821}/status"
