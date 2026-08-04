#!/bin/sh
# Maintain an SSH local-forward from this container to the pscontrol host:
#   listen 0.0.0.0:9822 (this container) -> ssh -> 127.0.0.1:9822 on SSH_TARGET
# which is the ps_exporter that `ps_run -a monitor` starts on pccmsbril06.
#
# autossh -M 0 relies on ServerAlive* to notice a dead link and reconnect, so a
# monitor restart on the far end (or a dropped VPN) heals without intervention.
set -eu
: "${SSH_TARGET:?set SSH_TARGET, e.g. mbrandt@pccmsbril06.cern.ch}"
: "${REMOTE_HOSTPORT:=127.0.0.1:9822}"
: "${LOCAL_PORT:=9822}"

exec autossh -M 0 -N \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile=/tmp/known_hosts \
    -o IdentitiesOnly=yes \
    -i /tunnel/id_ed25519 \
    -L "0.0.0.0:${LOCAL_PORT}:${REMOTE_HOSTPORT}" \
    "$SSH_TARGET"
