#!/usr/bin/env bash
# Copy the monitoring config to the OpenStack Prometheus host.
# Usage: ./deploy-monitoring.sh [user@host]   (default: prometheus-tk)
set -euo pipefail
TARGET="${1:-prometheus-tk}"
cd "$(dirname "$0")"
# Regenerate prometheus/scrape-config.yml from .env first - it is gitignored and
# not tracked, so it must be rendered fresh from the template on every deploy.
./monitoring/render.sh
# --delete keeps the host an exact mirror of monitoring/; the excludes keep
# local-only bits (.env, caches) off the box, matching deploy-psu-server.sh.
rsync -av --delete \
    --exclude='.env' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='tunnel/id_ed25519' --exclude='tunnel/id_ed25519.pub' \
    monitoring/ "$TARGET:/root/monitoring/"
echo
echo "Deployed monitoring/ to $TARGET:/root/monitoring/"
