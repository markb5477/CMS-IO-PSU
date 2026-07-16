#!/usr/bin/env bash
# Copy the PSU-server files to the lab PC. Usage: ./deploy-psu-server.sh [user@host]
set -euo pipefail
TARGET="${1:-xtaldaq@cmsladdertest.dyndns.cern.ch}"
cd "$(dirname "$0")"
# --delete keeps the lab PC an exact mirror of psu-server/; the excludes protect
# the machine's own .env and any experiment output/caches from being removed.
rsync -av --delete \
    --exclude='.env' --exclude='__pycache__' --exclude='*.pyc' --exclude='*.csv' \
    psu-server/ "$TARGET:cpx-psu-monitor/"
echo
echo "Deployed. ssh $TARGET, then see the repo README (On the lab PC)."
