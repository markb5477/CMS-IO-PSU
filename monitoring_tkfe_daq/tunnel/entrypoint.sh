#!/bin/sh
# Maintain an SSH local-forward from this container to the DAQ PC's exposer:
#   listen 0.0.0.0:9110 (this container) -> ssh -> $REMOTE_HOSTPORT on $SSH_TARGET
# Prometheus then scrapes "daq-tunnel:9110" as if it were a normal target.
#
# autossh -M 0 leans on ServerAlive* to notice a dead link and reconnect, which
# is the whole point on a beam-line network that drops connections.
set -eu
: "${SSH_TARGET:?set TUNNEL_SSH_TARGET in .env, e.g. user@daq-pc.cern.ch}"
: "${REMOTE_HOSTPORT:=127.0.0.1:9110}"
: "${LOCAL_PORT:=9110}"

# Word-split on purpose: TUNNEL_SSH_EXTRA_OPTS carries things like "-4 -J gw".
# shellcheck disable=SC2086
set -- -M 0 -N \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile=/tmp/known_hosts \
    -o IdentitiesOnly=yes \
    -o BatchMode=yes \
    -i /tunnel/id_ed25519 \
    ${SSH_EXTRA_OPTS:-} \
    -L "0.0.0.0:${LOCAL_PORT}:${REMOTE_HOSTPORT}"

echo "tunnel: 0.0.0.0:${LOCAL_PORT} -> ssh ${SSH_TARGET} -> ${REMOTE_HOSTPORT}"
exec autossh "$@" "$SSH_TARGET"
