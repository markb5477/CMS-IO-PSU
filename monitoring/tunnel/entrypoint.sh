#!/bin/sh
# Maintain an SSH local-forward from this container to the lab PC's exporter.
#   listen 0.0.0.0:9820 (this container)  ->  ssh  ->  127.0.0.1:9820 on SSH_TARGET
# autossh -M 0 relies on ServerAlive* to detect a dead link and reconnect.
set -eu
: "${SSH_TARGET:?set SSH_TARGET, e.g. xtaldaq@cmsladdertest.dyndns.cern.ch}"
: "${REMOTE_HOSTPORT:=127.0.0.1:9820}"
: "${LOCAL_PORT:=9820}"

exec autossh -M 0 -N \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile=/tmp/known_hosts \
    -o IdentitiesOnly=yes \
    -i /tunnel/id_ed25519 \
    -L "0.0.0.0:${LOCAL_PORT}:${REMOTE_HOSTPORT}" \
    "$SSH_TARGET"
