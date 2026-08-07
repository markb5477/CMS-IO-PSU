#!/usr/bin/env bash
# Full end-to-end deploy from off-site, through the lxtunnel bastion.
#
# Direct inbound SSH to the lab PC is blocked from outside CERN, so all three
# targets reach in by jumping through lxtunnel (configured in ~/.ssh/config):
#   psu-server/  as xtaldaq -> cmsladdertest:~/cpx-psu-monitor/   (PSU exporter)
#   bert-server/ as root    -> cmsladdertest:/opt/bert-monitor/   (BER exporter)
#   monitoring/  as root    -> prometheus-tk:/root/monitoring/
#
# lxtunnel wants a password + 2nd factor every connection. To pay that ONCE, we
# open a single multiplexed master to lxtunnel up front (ControlMaster is set for
# it in ~/.ssh/config); all three rsyncs jump through that shared socket with no
# further prompts. The master is closed on exit.
#
# Sending BER straight to /opt as root removes the old xtaldaq staging hop and the
# hand `cp -a` that once clobbered the root .env. --exclude=.env keeps every
# on-box .env (BERT_RESULTS_ROOT, PSU_HOST, ...) safe under --delete.
#
# This only moves files; it never touches services. The one manual step left is
# `systemctl restart bert-exporter` on the lab PC.
#
# Usage: ./deploy-remote.sh [--psu-only | --bert-only | --monitor-only]
# Env overrides: PSU_TARGET, BERT_TARGET, MONITOR_TARGET, BASTION
set -euo pipefail
cd "$(dirname "$0")"

PSU_TARGET="${PSU_TARGET:-xtaldaq@cmsladdertest.dyndns.cern.ch}"
BERT_TARGET="${BERT_TARGET:-root@cmsladdertest.dyndns.cern.ch}"
MONITOR_TARGET="${MONITOR_TARGET:-prometheus-tk}"
BASTION="${BASTION:-mbrandt@lxtunnel.cern.ch}"
DO_PSU=1; DO_BERT=1; DO_MON=1
case "${1:-}" in
    --psu-only)     DO_BERT=0; DO_MON=0 ;;
    --bert-only)    DO_PSU=0;  DO_MON=0 ;;
    --monitor-only) DO_PSU=0;  DO_BERT=0 ;;
    "")             ;;
    *) echo "usage: $0 [--psu-only|--bert-only|--monitor-only]" >&2; exit 2 ;;
esac

# Close the shared bastion master when we're done (or if we bail out).
cleanup() { ssh -O exit "$BASTION" 2>/dev/null || true; }
trap cleanup EXIT

# Authenticate to lxtunnel once: this prompt is the only password + 2FA you'll
# see. -fN backgrounds after auth and holds the master open for the rsyncs below.
# (Do NOT add GSSAPIAuthentication=no - CERN expects Kerberos and disabling it
# makes the bastion drop forwarded connections.)
echo "==> opening lxtunnel master (enter password + 2nd factor once)..."
ssh -fN "$BASTION"

EXC=(--exclude='.env' --exclude='__pycache__' --exclude='*.pyc' --exclude='*.csv')

if [ "$DO_PSU" = 1 ]; then
    echo "==> psu-server/  -> $PSU_TARGET:cpx-psu-monitor/"
    rsync -av --delete "${EXC[@]}" psu-server/ "$PSU_TARGET:cpx-psu-monitor/"
fi

if [ "$DO_BERT" = 1 ]; then
    echo "==> bert-server/ -> $BERT_TARGET:/opt/bert-monitor/"
    rsync -av --delete "${EXC[@]}" bert-server/ "$BERT_TARGET:/opt/bert-monitor/"
fi

if [ "$DO_MON" = 1 ]; then
    # prometheus.yml is gitignored - render it fresh from THIS machine's .env.
    ./monitoring/render.sh
    echo "==> monitoring/  -> $MONITOR_TARGET:/root/monitoring/"
    rsync -av --delete \
        --exclude='.env' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='storage' \
        --exclude='tunnel/id_ed25519' --exclude='tunnel/id_ed25519.pub' \
        monitoring/ "$MONITOR_TARGET:/root/monitoring/"
fi

echo
echo "Deployed."
[ "$DO_BERT" = 1 ] && cat <<'EOF'
Pick up the new BER exporter code (as root on the lab PC):
  systemctl restart bert-exporter
  curl -s localhost:9821/metrics | grep -E '^bert_(run_)?optical'
EOF
[ "$DO_PSU" = 1 ] && echo "PSU (as xtaldaq): systemctl --user restart cpx-exporter"
[ "$DO_MON" = 1 ] && echo "Grafana reloads the dashboard on its own within ~30s."
