#!/usr/bin/env bash
# Put the PS-log exporter on the pscontrol host (pccmsbril06).
#
# The exporter lives in Stian's repo (gitlab.cern.ch/mpari/pscontrol), not this
# one. This script only *copies* the two files into an existing checkout there:
#
#   src/ps_exporter.py    the exporter itself (new file)
#   src/run.py            + the 4-line hook that starts it from `ps_run -a monitor`
#
# It deliberately does NOT touch git on the far end: no commit, no push, no
# branch. Getting this upstream is a merge request on their repo and their call;
# this is the "have it running for the testbeam now" path, and it is reversible
# with `git checkout src/run.py && rm src/ps_exporter.py` in that checkout.
#
# run.py is backed up before being overwritten, because it is a file THEY own and
# may have edited since our copy was taken.
#
# Nothing restarts by itself: the exporter only comes up on the next
# `ps_run -a monitor` (or `systemctl restart ps-monitor`), which is a decision
# about the power supplies and so is left to a human.
#
# Usage: ./deploy-ps-exporter.sh [--dry-run]
# Env overrides: PSCONTROL_TARGET, PSCONTROL_DIR, PSCONTROL_SRC
set -euo pipefail
cd "$(dirname "$0")"

TARGET="${PSCONTROL_TARGET:-mbrandt@pccmsbril06.cern.ch}"
REMOTE_DIR="${PSCONTROL_DIR:-/home/Software/pscontrol}"
LOCAL_SRC="${PSCONTROL_SRC:-$HOME/Documents/Git/pscontrol}"
DRY=()
[ "${1:-}" = "--dry-run" ] && DRY=(--dry-run) && echo "== DRY RUN =="

for f in src/ps_exporter.py src/run.py; do
    [ -f "$LOCAL_SRC/$f" ] || { echo "error: $LOCAL_SRC/$f not found" >&2; exit 1; }
done

# The hook has to actually be in the run.py we are about to ship, or the exporter
# would land on the far end and never be started by anything.
grep -q 'ps_exporter.serve' "$LOCAL_SRC/src/run.py" || {
    echo "error: $LOCAL_SRC/src/run.py has no ps_exporter.serve() call - wrong copy?" >&2
    exit 1
}

echo "==> running the exporter's tests before shipping it"
( cd "$LOCAL_SRC/src" && python3 -m unittest discover -s tests -q ) \
    || { echo "error: tests failed, refusing to deploy" >&2; exit 1; }

echo "==> $LOCAL_SRC/src/{ps_exporter.py,run.py} -> $TARGET:$REMOTE_DIR/src/"
if [ ${#DRY[@]} -eq 0 ]; then
    # One connection: back the file up, then take both files on stdin.
    ssh "$TARGET" "set -e
        cd '$REMOTE_DIR/src'
        [ -f run.py ] && cp -a run.py \"run.py.bak-\$(date +%Y%m%d-%H%M%S)\"
        echo '    backed up their run.py'"
    rsync -av "$LOCAL_SRC/src/ps_exporter.py" "$LOCAL_SRC/src/run.py" \
        "$TARGET:$REMOTE_DIR/src/"
    rsync -av --exclude='__pycache__' --exclude='*.pyc' \
        "$LOCAL_SRC/src/tests/" "$TARGET:$REMOTE_DIR/src/tests/"
else
    rsync -avn "$LOCAL_SRC/src/ps_exporter.py" "$LOCAL_SRC/src/run.py" \
        "$TARGET:$REMOTE_DIR/src/"
fi

[ ${#DRY[@]} -gt 0 ] && { echo; echo "dry run only."; exit 0; }

cat <<EOF

Deployed to $TARGET:$REMOTE_DIR/src/

Sanity-check it there without touching the supplies:
  ssh $TARGET
  cd $REMOTE_DIR/src && python3 -m unittest discover -s tests -q
  git -C $REMOTE_DIR status            # see exactly what changed in their checkout

Then the exporter starts with the next monitor run:
  sudo systemctl restart ps-monitor     # or: ps_run -a monitor
  curl -s localhost:9822/status | head

To undo, in $REMOTE_DIR:
  git checkout src/run.py && rm src/ps_exporter.py && rm -rf src/tests
EOF
