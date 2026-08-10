#!/usr/bin/env bash
# Turn OFF the stack. Prometheus, Grafana and the (optional) daq-tunnel sidecar
# all come down together.
#
# DATA IS NOT DELETED. The week of history stays in the prometheus-data volume,
# hot and immediately queryable again on the next ./on.sh.
#
# Pass --wipe to ALSO reset the volumes (fresh start, history gone for good).
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a

# --profile tunnel so the sidecar is included in `down` even when it was started
# by an earlier run with USE_TUNNEL=yes and .env has since been flipped to no.
if [ "${1:-}" = "--wipe" ]; then
    read -r -p "This DELETES the recorded DAQ history (${PROM_RETENTION:-7d} of it). Type 'wipe' to confirm: " ans
    [ "$ans" = "wipe" ] || { echo "aborted, nothing changed."; exit 1; }
    docker compose --profile tunnel down --volumes --remove-orphans
    echo "stopped, and RESET the volumes - Prometheus starts empty next time."
elif [ -n "${1:-}" ]; then
    echo "usage: $0 [--wipe]" >&2; exit 2
else
    docker compose --profile tunnel down --remove-orphans
    echo "stopped prometheus + grafana + tunnel. History KEPT in the volume; ./on.sh brings it straight back."
fi

left="$(docker ps --filter 'name=tkfe-' --format '{{.Names}}' || true)"
[ -n "$left" ] && echo "warning: still running: $left" || echo "confirmed: no tkfe-* containers running."
