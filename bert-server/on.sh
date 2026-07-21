#!/usr/bin/env bash
# Turn ON the BER monitoring (bert-exporter) on the LAB PC.
# This is what actually watches bertContinuous.csv and serves it on :9821 for the
# monitor PC to scrape. See bert-server/README.md for the one-time install.
set -euo pipefail
cd "$(dirname "$0")"

# Load BERT_HTTP_PORT (and friends) if a local .env is present, so we print the
# right port; the service reads the same .env itself.
[ -f .env ] && set -a && . ./.env && set +a

# Two supported installs, see README: a per-user service (when the DAQ's Results/
# is readable by the deploy user) or a root system service (when it lives under
# /root). Use whichever unit is actually installed rather than assuming.
if systemctl cat bert-exporter.service >/dev/null 2>&1; then
    SCOPE=--system
else
    SCOPE=--user
fi

if ! systemctl "$SCOPE" start bert-exporter 2>/dev/null; then
    echo "could not start bert-exporter ($SCOPE scope)." >&2
    [ "$SCOPE" = --system ] && echo "the system unit needs root: try 'sudo ./on.sh'." >&2
    [ "$SCOPE" = --user ] && echo "no unit installed - see the Deploy section of README.md." >&2
    exit 1
fi
systemctl "$SCOPE" --no-pager status bert-exporter | head -5
echo
echo "metrics: curl -s localhost:${BERT_HTTP_PORT:-9821}/metrics | head"
echo "status : curl -s localhost:${BERT_HTTP_PORT:-9821}/status"
