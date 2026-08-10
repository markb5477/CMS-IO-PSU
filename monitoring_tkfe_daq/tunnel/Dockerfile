# Tiny autossh sidecar: holds an SSH connection to the DAQ PC and republishes
# its 127.0.0.1:9110 exposer on the compose network as host "daq-tunnel".
# Only built/started when USE_TUNNEL=yes (compose profile "tunnel").
FROM alpine:3.20
RUN apk add --no-cache autossh openssh-client
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
