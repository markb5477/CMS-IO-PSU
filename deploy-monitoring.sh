#!/usr/bin/env bash
# Copy the monitoring config to the OpenStack Prometheus host.
# Usage: ./deploy-monitoring.sh [user@host]   (default: prometheus-tk)
set -euo pipefail
TARGET="${1:-prometheus-tk}"
cd "$(dirname "$0")"
# Regenerate prometheus/prometheus.yml from .env first - it is gitignored and not
# tracked, so it must be rendered fresh from the template on every deploy. Note
# this uses THIS machine's monitoring/.env, not the one on the box.
./monitoring/render.sh
# --delete keeps the host an exact mirror of monitoring/; the excludes keep
# local-only bits (.env, caches) off the box, matching deploy-psu-server.sh.
# storage/ is excluded because it holds the TSDB backup tarballs written on the
# BOX by archive-tsdb.sh. An --exclude'd path is also shielded from --delete on
# the receiver, so without this line every deploy would wipe those archives
# (this dir is empty here, so --delete would mirror "empty" onto the host).
rsync -av --delete \
    --exclude='.env' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='storage' \
    --exclude='tunnel/id_ed25519' --exclude='tunnel/id_ed25519.pub' \
    monitoring/ "$TARGET:/root/monitoring/"
echo
echo "Deployed monitoring/ to $TARGET:/root/monitoring/"
