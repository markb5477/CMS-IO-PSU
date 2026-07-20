#!/usr/bin/env bash
# Turn OFF the monitoring stack: Prometheus, Grafana AND the psu-tunnel reverse
# SSH sidecar all come down together (the tunnel's ssh runs inside its container,
# so it dies with it - this also closes the inbound session on the lab PC, and
# with it both the PSU and BER scrape paths).
#
# DATA IS NEVER DELETED HERE. The TSDB stays in its named volume, hot and
# immediately queryable again on the next ./on.sh. A backup archive is ALSO
# written to ./storage on every off (via archive-tsdb.sh) - insurance only;
# overlapping on/off archives may duplicate data, which is fine.
#
# Pass --wipe to ALSO reset the volume for a fresh start. The archive is taken
# first either way, so even a wipe preserves the recorded data.
set -euo pipefail
cd "$(dirname "$0")"

# Load PROMETHEUS_PORT (and friends) if present.
[ -f .env ] && set -a && . ./.env && set +a

# Always back up the TSDB first (does not delete anything).
./archive-tsdb.sh off || echo "warning: TSDB archive step failed; continuing with shutdown (live data is still kept)."

if [ "${1:-}" != "--wipe" ]; then
    docker compose down --remove-orphans
    echo "stopped prometheus + grafana + psu-tunnel (PSU + BER scrape paths)."
    echo "data KEPT in the hot TSDB volume (also archived to ./storage). ./off.sh --wipe to reset the volume."
else
    # Everything is archived above; now tear down AND reset the volumes.
    docker compose down --volumes --remove-orphans
    echo "stopped prometheus + grafana + psu-tunnel (PSU + BER scrape paths) and RESET the volume (data was archived to ./storage first)."
fi

# sanity: confirm no project container is left running
left="$(docker ps --filter 'name=cpx-' --format '{{.Names}}' || true)"
if [ -n "$left" ]; then
    echo "warning: project containers still up: $left"
else
    echo "confirmed: no cpx-* containers running (reverse SSH tunnel torn down with its container)."
fi
