#!/usr/bin/env bash
# Copy the lab-PC files to cmsladdertest. Usage: ./deploy-psu-server.sh [user@host]
#
# Deploys BOTH exporters, which live on the same machine:
#   psu-server/  -> ~/cpx-psu-monitor/   PSU exporter (:9820) + psuctl + experiments
#   bert-server/ -> ~/bert-monitor/      BER exporter  (:9821), follows bertContinuous.csv
set -euo pipefail
TARGET="${1:-xtaldaq@cmsladdertest.dyndns.cern.ch}"
cd "$(dirname "$0")"

# --delete keeps the lab PC an exact mirror of the source dir; the excludes
# protect the machine's own .env and any output/caches from being removed.
# (An --exclude'd path is also shielded from --delete on the receiver, so the
# on-site .env survives every deploy - it is where the PSU host and the BER CSV
# path are configured.)
COMMON_EXCLUDES=(
    --exclude='.env'
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='*.csv'
)

echo "==> psu-server/ -> $TARGET:cpx-psu-monitor/"
rsync -av --delete "${COMMON_EXCLUDES[@]}" \
    psu-server/ "$TARGET:cpx-psu-monitor/"

echo
echo "==> bert-server/ -> $TARGET:bert-monitor/"
rsync -av --delete "${COMMON_EXCLUDES[@]}" \
    bert-server/ "$TARGET:bert-monitor/"

echo
echo "Deployed both exporters. ssh $TARGET, then:"
echo "  cd ~/cpx-psu-monitor && cp -n .env.example .env && \$EDITOR .env && ./on.sh   # PSU  :9820"
echo "  cd ~/bert-monitor    && cp -n .env.example .env && \$EDITOR .env && ./on.sh   # BER  :9821"
echo
echo "The BER .env is where BERT_RESULTS_ROOT / BERT_CSV are set - see the repo"
echo "README (Pointing the BER exporter at the CSV). Verify with:"
echo "  curl -s localhost:9820/metrics | grep '^cpx_up'"
echo "  curl -s localhost:9821/status  | grep -m1 path"
