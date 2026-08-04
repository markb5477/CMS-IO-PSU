#!/usr/bin/env bash
# Turn OFF the pscontrol PS-log monitoring stack. Data stays in the named volumes
# and comes back hot on the next ./on.sh. Never touches the power supplies, the
# monitor, or the ../monitoring (CPX) stack.
set -euo pipefail
cd "$(dirname "$0")"
docker compose --profile tunnel down
echo "stopped. TSDB and Grafana state kept in the pslog-monitoring volumes."
