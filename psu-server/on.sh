#!/usr/bin/env bash
# Turn ON the PSU monitoring (cpx-exporter) on the lab PC.
# Uses the per-user systemd service (no sudo). See "On the lab PC" in the README
# for the one-time install of ~/.config/systemd/user/cpx-exporter.service.
set -euo pipefail
systemctl --user start cpx-exporter
systemctl --user --no-pager status cpx-exporter | head -5
echo
echo "metrics: curl -s localhost:${HTTP_PORT:-9820}/metrics | head"
