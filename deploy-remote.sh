#!/usr/bin/env bash
# Full end-to-end deploy over ONE authenticated SSH connection per host.
#
# Why this exists: the plain deploy-*.sh scripts open a separate SSH session for
# each rsync. Off-site that means the lxtunnel jump host asks for your 2nd factor
# on EVERY transfer (three rsyncs = three OTP prompts), and the extra handshakes
# to cmsladdertest are what was stalling. Here we open one multiplexed master
# connection per target - authenticate (and 2FA) exactly once - run every rsync
# over it, then close it on exit.
#
# It deploys the same files as deploy-psu-server.sh + deploy-monitoring.sh, but
# to the user each half actually needs:
#   psu-server/  as xtaldaq -> ~/cpx-psu-monitor/    (PSU exporter, user service)
#   bert-server/ as root    -> /opt/bert-monitor/    (BER exporter, root service)
#   monitoring/  as root    -> prometheus-tk:/root/monitoring/
# Sending BER straight to /opt as root removes the old xtaldaq staging hop and the
# hand `cp -a` that once clobbered the root .env. --exclude=.env keeps the on-box
# config (BERT_RESULTS_ROOT, PSU_HOST, ...) safe under --delete.
#
# It only reads/writes files and never touches services. The single remaining
# manual step is `systemctl restart bert-exporter` on the lab PC.
#
# Usage: ./deploy-remote.sh [--psu-only | --bert-only | --monitor-only]
# Env overrides:
#   PSU_TARGET     (default xtaldaq@cmsladdertest.dyndns.cern.ch)
#   BERT_TARGET    (default root@cmsladdertest.dyndns.cern.ch)
#   MONITOR_TARGET (default prometheus-tk)
set -euo pipefail
cd "$(dirname "$0")"

PSU_TARGET="${PSU_TARGET:-xtaldaq@cmsladdertest.dyndns.cern.ch}"
BERT_TARGET="${BERT_TARGET:-root@cmsladdertest.dyndns.cern.ch}"
MONITOR_TARGET="${MONITOR_TARGET:-prometheus-tk}"
DO_PSU=1; DO_BERT=1; DO_MON=1
case "${1:-}" in
    --psu-only)     DO_BERT=0; DO_MON=0 ;;
    --bert-only)    DO_PSU=0;  DO_MON=0 ;;
    --monitor-only) DO_PSU=0;  DO_BERT=0 ;;
    "")             ;;
    *) echo "usage: $0 [--psu-only|--bert-only|--monitor-only]" >&2; exit 2 ;;
esac

# One control socket per target, in a private dir cleaned up on exit.
CTL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cms-deploy.XXXXXX")"
CTL_PATH="$CTL_DIR/%r@%h:%p"
MASTERS=()
cleanup() {
    for t in "${MASTERS[@]:-}"; do
        ssh -o ControlPath="$CTL_PATH" -O exit "$t" 2>/dev/null || true
    done
    rm -rf "$CTL_DIR"
}
trap cleanup EXIT

# Open a background master to $1. Authenticate + 2FA happen here, once; every
# ssh/rsync to the same target afterwards reuses the socket with no prompt.
# extra args ($2..) are passed to ssh (e.g. -4 to force IPv4 past a black-holing
# AAAA record - the case for cmsladdertest's dyndns name).
open_master() {
    local target="$1"; shift
    echo "==> opening shared connection to $target (authenticate once)..."
    ssh "$@" -o ControlMaster=yes -o ControlPath="$CTL_PATH" \
        -o ControlPersist=600 -o ServerAliveInterval=15 -MNf "$target"
    MASTERS+=("$target")
}

# rsync over whichever master socket matches the target host.
rr() { rsync -av -e "ssh -o ControlPath=$CTL_PATH" "$@"; }

EXC=(--exclude='.env' --exclude='__pycache__' --exclude='*.pyc')

# -4 on both lab connections: the dyndns name publishes an AAAA that black-holes,
# so force IPv4. PSU and BER are separate users, hence separate master sockets.
if [ "$DO_PSU" = 1 ]; then
    open_master "$PSU_TARGET" -4
    echo "==> psu-server/  -> $PSU_TARGET:cpx-psu-monitor/"
    rr --delete "${EXC[@]}" --exclude='*.csv' psu-server/ "$PSU_TARGET:cpx-psu-monitor/"
fi

if [ "$DO_BERT" = 1 ]; then
    open_master "$BERT_TARGET" -4
    # Straight into the live root service dir; --exclude=.env (in EXC) keeps the
    # on-box BERT_RESULTS_ROOT under --delete. No xtaldaq staging hop.
    echo "==> bert-server/ -> $BERT_TARGET:/opt/bert-monitor/"
    rr --delete "${EXC[@]}" --exclude='*.csv' bert-server/ "$BERT_TARGET:/opt/bert-monitor/"
fi

if [ "$DO_MON" = 1 ]; then
    # prometheus.yml is gitignored - render it fresh from THIS machine's .env.
    ./monitoring/render.sh
    open_master "$MONITOR_TARGET"
    echo "==> monitoring/  -> $MONITOR_TARGET:/root/monitoring/"
    rr --delete "${EXC[@]}" \
       --exclude='storage' \
       --exclude='tunnel/id_ed25519' --exclude='tunnel/id_ed25519.pub' \
       monitoring/ "$MONITOR_TARGET:/root/monitoring/"
fi

echo
echo "Deployed."
[ "$DO_BERT" = 1 ] && cat <<EOF
Pick up the new BER exporter code (as root on the lab PC):
  systemctl restart bert-exporter
  curl -s localhost:9821/metrics | grep -E '^bert_(run_)?optical'
EOF
[ "$DO_PSU" = 1 ] && echo "PSU (as xtaldaq): systemctl --user restart cpx-exporter"
[ "$DO_MON" = 1 ] && echo "Grafana reloads the dashboard on its own within ~30s."
