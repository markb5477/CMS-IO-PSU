# Tiny autossh sidecar: holds an SSH reverse path to the lab PC and republishes
# the exporter's localhost:9820 on the compose network as host "psu-tunnel".
FROM alpine:3.20
RUN apk add --no-cache autossh openssh-client
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
