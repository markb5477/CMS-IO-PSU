#!/usr/bin/env bash
# What is actually running, and what config is actually loaded.
#
# Answers "is the chain healthy, and is Prometheus using what I think it is?" by
# asking the running processes rather than reading the files on disk - those two
# drift apart the moment someone edits a file without ./render.sh + a reload.
#
# Read-only: starts, stops and changes nothing.
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
PROM="localhost:${PROMETHEUS_PORT:-9091}"
GRAF="localhost:${GRAFANA_PORT:-3001}"

ok()   { printf '  \033[32m OK \033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }

echo
echo "=== containers ==="
docker compose --profile tunnel ps --format '  {{.Name}}  {{.Status}}' 2>/dev/null \
    || bad "docker compose not answering"

echo
echo "=== the chain, end to end ==="

# 1. the far end: is anything actually serving the values log?
if docker inspect -f '{{.State.Running}}' pslog-tunnel 2>/dev/null | grep -q true; then
    if docker exec pslog-prometheus wget -q -O- -T3 http://pslog-tunnel:9822/metrics >/dev/null 2>&1
    then ok "exporter answering at the far end (through the tunnel)"
    else
        bad "nothing answering on :9822 at the far end"
        echo "         the exporter is not deployed there, or the monitor is not running."
        echo "         it starts with: ps_run -a monitor  /  systemctl restart ps-monitor"
    fi
else
    warn "tunnel container not running (direct scrape? check PSLOG_EXPORTER_TARGET)"
fi

# 2. Prometheus: is the scrape working, and what did it last say?
TARGETS=$(curl -s "$PROM/api/v1/targets" 2>/dev/null || true)
HEALTH=$(TARGETS="$TARGETS" python3 - <<'PY' 2>/dev/null
import json, os
try:
    t = json.loads(os.environ["TARGETS"])["data"]["activeTargets"]
except Exception:
    print("unreachable|"); raise SystemExit
if not t:
    print("none|no targets configured")
for x in t:
    print(x["health"] + "|" + (x["lastError"] or ""))
PY
)
case "${HEALTH%%|*}" in
    up)          ok "Prometheus is scraping the exporter" ;;
    down)        bad "scrape target DOWN: ${HEALTH#*|}" ;;
    unreachable) bad "Prometheus not answering on $PROM" ;;
    *)           bad "scrape target: ${HEALTH:-unknown}" ;;
esac

# 3. is data actually landing in the TSDB?
ROWS=$(curl -s "$PROM/api/v1/query?query=pslog_file_rows" 2>/dev/null \
       | python3 -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "")' 2>/dev/null)
if [ -n "$ROWS" ]; then ok "data landing: $ROWS rows parsed from the current log"
else bad "no pslog_* samples in Prometheus yet"; fi

# 4. Grafana. Separate "wrong login" from "no dashboards": they look identical on
# /api/search (both yield nothing usable) but have completely different fixes.
if curl -sf "$GRAF/api/health" >/dev/null 2>&1; then
    GUSER="${GRAFANA_ADMIN_USER:-username}"
    BODY=$(mktemp)
    CODE=$(curl -s -o "$BODY" -w '%{http_code}' \
        -u "$GUSER:${GRAFANA_ADMIN_PASSWORD:-password}" "$GRAF/api/search?query=" 2>/dev/null)
    case "$CODE" in
        200)
            N=$(grep -c '"type":"dash-db"' "$BODY" || true)
            if [ "${N:-0}" -gt 0 ] 2>/dev/null
            then ok "Grafana up, ${N} dashboard(s) provisioned"
            else
                warn "Grafana up, login OK, but NO dashboard loaded"
                echo "         docker logs pslog-grafana 2>&1 | grep -i 'provision\|dashboard'"
            fi ;;
        401|403)
            warn "Grafana up but the login in .env is rejected (HTTP $CODE, user '$GUSER')"
            echo "         Grafana seeds its admin account on FIRST start only, so a"
            echo "         password changed in .env afterwards does NOT take effect."
            echo "         reset it:  docker exec -it pslog-grafana grafana cli \\"
            echo "                      admin reset-admin-password '<new>'"
            echo "         or wipe and re-seed from .env:"
            echo "                    ./off.sh && docker volume rm pslog-monitoring_grafana-data && ./on.sh" ;;
        *)  warn "Grafana /api/search returned HTTP $CODE" ;;
    esac
    rm -f "$BODY"
else
    bad "Grafana not answering on $GRAF"
fi

echo
echo "=== config Prometheus has actually LOADED ==="
echo "    (the RUNNING config, not the file on disk - they differ if you edited"
echo "     prometheus.yml without ./render.sh and a reload)"
CFG=$(curl -s "$PROM/api/v1/status/config" 2>/dev/null || true)
CFG="$CFG" python3 - <<'PY' 2>/dev/null
import json, os, re
try:
    cfg = json.loads(os.environ["CFG"])["data"]["yaml"]
except Exception:
    print("    <Prometheus not answering>"); raise SystemExit
job = re.search(r"job_name:\s*(\S+)", cfg)
ivl = re.findall(r"scrape_interval:\s*(\S+)", cfg)
tgt = re.search(r"targets:\s*\n\s*-\s*(\S+)", cfg)      # target is on the NEXT line
lbl = dict(re.findall(r"^\s*(setup|location):\s*(\S+)", cfg, re.M))
print("    job           ", job.group(1) if job else "<none>")
print("    target        ", tgt.group(1).strip('"\'') if tgt else "<none>")
print("    scrape every  ", ivl[-1] if ivl else "?")
print("    labels         setup=%s location=%s" % (lbl.get("setup", "?"), lbl.get("location", "?")))
PY

DISK=$(grep -oP '(?<=targets: \[")[^"]+' prometheus/prometheus.yml 2>/dev/null || true)
LIVE=$(printf '%s' "$CFG" | grep -oE '[A-Za-z0-9._-]+:9822' | head -1 || true)
if [ -n "$DISK" ] && [ -n "$LIVE" ] && [ "$DISK" != "$LIVE" ]; then
    warn "prometheus.yml on disk says '$DISK' but the RUNNING config says '$LIVE'"
    echo "         ./render.sh && curl -X POST $PROM/-/reload"
fi

echo
echo "=== alert rules loaded ==="
RULES=$(curl -s "$PROM/api/v1/rules" 2>/dev/null || true)
RULES="$RULES" python3 - <<'PY' 2>/dev/null
import json, os
try:
    groups = json.loads(os.environ["RULES"])["data"]["groups"]
except Exception:
    print("    <none loaded>"); raise SystemExit
total = active = 0
for g in groups:
    for r in g["rules"]:
        total += 1
        state = r.get("state", "?")
        if state != "inactive":
            active += 1
            mark = "[FIRING] " if state == "firing" else "[pending]"
            print("    %s %s" % (mark, r["name"]))
print("    %d rules loaded, %d firing/pending" % (total, active))
PY

cat <<EOF

=== changing what Prometheus uses ===
  scrape target / interval / labels : edit .env, then
                                      ./render.sh && curl -X POST $PROM/-/reload
  alert rules                       : edit prometheus/alerts.yml, then
                                      curl -X POST $PROM/-/reload
  dashboard                         : edit grafana/dashboards/pslog/*.json
                                      (Grafana re-reads within 30s, no reload)
  ports / Grafana login             : edit .env, then ./on.sh
  restart everything                : ./off.sh && ./on.sh  (TSDB kept in volumes)

  A reload is atomic: a bad config is REJECTED and the old one keeps running, so
  a typo cannot take monitoring down. Re-run ./status.sh to confirm it took.
EOF
