#!/usr/bin/env bash
# Bake .env into prometheus/prometheus.yml.
#
# Run this after ANY edit to .env that touches a scrape value (targets, interval,
# labels). Prometheus reads its config from a file, not from the environment, so
# without this step the container comes up with the previous target and looks
# broken for no visible reason.
#
#   ./render.sh            write prometheus/prometheus.yml
#   ./render.sh && ./reload.sh   apply it to an already-running Prometheus
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { echo "error: no .env here. Run:  cp .env.example .env  and edit it." >&2; exit 1; }
set -a; . ./.env; set +a

# Fail loudly and specifically rather than writing a config with an empty field.
for v in DAQ_EXPORTER_TARGETS DAQ_SCRAPE_INTERVAL DAQ_SCRAPE_TIMEOUT SETUP LOCATION BEAMTEST; do
    [ -n "${!v:-}" ] || { echo "error: $v is empty in .env - see the comments in .env.example" >&2; exit 1; }
done

# DAQ_EXPORTER_TARGETS is a comma-separated list so one .env line can cover
# several DAQ PCs; expand it into the YAML block the template expects.
target_lines=""
n=0
IFS=',' read -r -a _targets <<< "$DAQ_EXPORTER_TARGETS"
for t in ${_targets[@]+"${_targets[@]}"}; do
    t="${t#"${t%%[![:space:]]*}"}"   # trim leading space
    t="${t%"${t##*[![:space:]]}"}"   # trim trailing space
    [ -n "$t" ] || continue
    case "$t" in
        *:*) ;;
        *) echo "error: target '$t' has no port - write it as host:port, e.g. $t:9110" >&2; exit 1 ;;
    esac
    target_lines+="          - \"$t\""$'\n'
    n=$((n + 1))
done
[ "$n" -gt 0 ] || { echo "error: DAQ_EXPORTER_TARGETS parsed to nothing" >&2; exit 1; }
export DAQ_TARGET_LINES="${target_lines%$'\n'}"

command -v envsubst >/dev/null || { echo "error: envsubst missing - run ./install.sh (package: gettext)" >&2; exit 1; }
envsubst < prometheus/prometheus.yml.tmpl > prometheus/prometheus.yml

echo "wrote prometheus/prometheus.yml"
echo "  $n scrape target(s): $DAQ_EXPORTER_TARGETS"
echo "  every $DAQ_SCRAPE_INTERVAL (timeout $DAQ_SCRAPE_TIMEOUT), labelled setup=$SETUP location=$LOCATION beamtest=$BEAMTEST"
if [ "${USE_TUNNEL:-no}" = "yes" ] && ! grep -q 'daq-tunnel' prometheus/prometheus.yml; then
    echo "  warning: USE_TUNNEL=yes but no target points at daq-tunnel:9110 - the sidecar will run unused."
fi
