#!/usr/bin/env bash
# Deploy monitoring_beam_test/ to the Prometheus+Grafana node.
#
#   monitoring_beam_test/ -> prometheus-tk:/root/monitoring_beam_test/
#
# lxtunnel wants a password + 2nd factor on every connection, so we open ONE
# multiplexed master up front and run the rsync through it.
#
# deploy-remote.sh assumes ~/.ssh/config sets ControlMaster for lxtunnel; it does
# not (that block only sets AddressFamily), which is why deploys prompt twice.
# Rather than edit a personal config from a repo script, we generate a throwaway
# config that adds the multiplexing and then Includes the real one - ssh takes
# the FIRST value it sees for a key, so these win and everything else (ProxyJump,
# IdentityFile, ...) still comes from ~/.ssh/config. It matters that the setting
# lands in a *config file*: the rsync reaches prometheus-tk via ProxyJump, and
# that inner hop to lxtunnel is a separate ssh process which -o on our command
# line would never reach.
#
# This node already runs the CPX stack out of /root/monitoring on :9090/:3000.
# This one is a SEPARATE compose project on :9091/:3001 with its own volumes, so
# deploying and starting it cannot disturb that stack - nothing here writes
# outside /root/monitoring_beam_test.
#
# Files only; no service is started. The remote bootstrap is printed at the end.
#
# Usage: ./deploy-beam-test.sh [--dry-run]
# Env overrides: MONITOR_TARGET, BASTION, REMOTE_DIR
set -euo pipefail
cd "$(dirname "$0")"

MONITOR_TARGET="${MONITOR_TARGET:-prometheus-tk}"
BASTION="${BASTION:-mbrandt@lxtunnel.cern.ch}"
REMOTE_DIR="${REMOTE_DIR:-/root/monitoring_beam_test}"
DRY=()
[ "${1:-}" = "--dry-run" ] && DRY=(--dry-run) && echo "== DRY RUN, nothing will be written =="

[ -f monitoring_beam_test/.env ] || {
    echo "error: monitoring_beam_test/.env missing - it is what prometheus.yml is rendered from." >&2
    echo "       cp monitoring_beam_test/.env.example monitoring_beam_test/.env and set the scrape target." >&2
    exit 1
}

# prometheus.yml is gitignored: render it fresh so the shipped config matches the
# target in .env rather than whatever was last rendered.
./monitoring_beam_test/render.sh

# Sanity-check what we are about to ship, so a typo is caught here and not by a
# container that then crash-loops on the far end.
docker run -i --rm --entrypoint promtool \
    -v "$PWD/monitoring_beam_test/prometheus:/etc/prometheus:ro" \
    prom/prometheus check config /etc/prometheus/prometheus.yml >/dev/null \
    && echo "==> prometheus.yml + alerts.yml validate"

TARGET_IN_CFG=$(grep -oP '(?<=targets: \[")[^"]+' monitoring_beam_test/prometheus/prometheus.yml)
echo "==> shipping with scrape target: $TARGET_IN_CFG"
case "$TARGET_IN_CFG" in
    host.docker.internal:*)
        echo "    WARNING: that is the LOCAL REPLAY target. On the node it will scrape nothing." >&2
        echo "             Set PSLOG_EXPORTER_TARGET in .env to pslog-tunnel:9822 (or the host directly)." >&2
        read -rp "    ship it anyway? [y/N] " ans
        [ "${ans:-N}" = y ] || exit 1 ;;
esac

SSH_CONF=$(mktemp)
SOCK="${TMPDIR:-/tmp}/beamdeploy-%r@%h:%p"
# The Include MUST sit under `Host *`: an Include inside the lxtunnel block is
# only processed for that host, which would leave prometheus-tk with no ProxyJump.
cat > "$SSH_CONF" <<CONF
Host lxtunnel.cern.ch
    ControlMaster auto
    ControlPath $SOCK
    ControlPersist 120

Host *
    Include ~/.ssh/config
CONF

# Close the shared bastion master and drop the temp config on the way out.
cleanup() {
    ssh -F "$SSH_CONF" -O exit "$BASTION" 2>/dev/null || true
    rm -f "$SSH_CONF"
}
trap cleanup EXIT

echo "==> opening lxtunnel master (enter password + 2nd factor ONCE)..."
ssh -F "$SSH_CONF" -fN "$BASTION"

echo "==> monitoring_beam_test/ -> $MONITOR_TARGET:$REMOTE_DIR/"
# --delete keeps the far end an exact mirror, with three carve-outs:
#   .env               the node's own ports/password, must survive a redeploy
#   tunnel/id_*        the SSH key, generated ON the node and never sent from here
#   .playwright-mcp    local screenshot scratch
rsync -av "${DRY[@]}" --delete \
    -e "ssh -F $SSH_CONF" \
    --exclude='.env' \
    --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='tunnel/id_ed25519' --exclude='tunnel/id_ed25519.pub' \
    --exclude='.playwright-mcp' \
    monitoring_beam_test/ "$MONITOR_TARGET:$REMOTE_DIR/"

[ ${#DRY[@]} -gt 0 ] && { echo; echo "dry run only."; exit 0; }

cat <<EOF

Deployed to $MONITOR_TARGET:$REMOTE_DIR

First time on the node (ssh prometheus-tk):
  cd $REMOTE_DIR
  cp .env.example .env
  \$EDITOR .env                 # set GRAFANA_ADMIN_PASSWORD; check the scrape target
  ./render.sh                   # re-render with the node's own .env
  ./on.sh

  # only if scraping via the sidecar (PSLOG_EXPORTER_TARGET=pslog-tunnel:9822):
  ssh-keygen -t ed25519 -N '' -f tunnel/id_ed25519
  ssh-copy-id -i tunnel/id_ed25519.pub mbrandt@pccmsbril06.cern.ch

Check it:
  curl -s localhost:9091/api/v1/targets | grep -o '"health":"[a-z]*"'
  Grafana http://localhost:3001 -> PS Monitoring

Redeploys keep the node's .env and SSH key (both excluded above).
EOF
