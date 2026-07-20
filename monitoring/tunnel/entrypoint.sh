#!/bin/sh
# Maintain SSH local-forward(s) from this container to the lab PC's exporters.
#   listen 0.0.0.0:9820 (this container)  ->  ssh  ->  127.0.0.1:9820 on SSH_TARGET  (PSU exporter)
#   listen 0.0.0.0:9821 (optional)        ->  ssh  ->  127.0.0.1:9821 on SSH_TARGET  (BERT exporter)
# Both forwards ride the SAME ssh connection (same host). autossh -M 0 relies on
# ServerAlive* to detect a dead link and reconnect.
set -eu
: "${SSH_TARGET:?set SSH_TARGET, e.g. xtaldaq@cmsladdertest.dyndns.cern.ch}"
: "${REMOTE_HOSTPORT:=127.0.0.1:9820}"
: "${LOCAL_PORT:=9820}"

set -- -M 0 -N \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile=/tmp/known_hosts \
    -o IdentitiesOnly=yes \
    -i /tunnel/id_ed25519 \
    -L "0.0.0.0:${LOCAL_PORT}:${REMOTE_HOSTPORT}"

# Optional second forward for the BERT status exporter. Unset -> behaves exactly
# as before (PSU-only), so this is backward compatible.
if [ -n "${BERT_LOCAL_PORT:-}" ]; then
    : "${BERT_REMOTE_HOSTPORT:=127.0.0.1:9821}"
    set -- "$@" -L "0.0.0.0:${BERT_LOCAL_PORT}:${BERT_REMOTE_HOSTPORT}"
fi

exec autossh "$@" "$SSH_TARGET"
