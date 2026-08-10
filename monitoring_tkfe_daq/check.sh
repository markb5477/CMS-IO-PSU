#!/usr/bin/env bash
##############################################################################
# NETWORK / END-TO-END DIAGNOSTIC.  Run this whenever anything looks wrong.
#
#   ./check.sh          one full pass, prints a verdict per hop
#   ./check.sh --watch  re-run every 5 s (control-room screen)
#
# It walks the whole chain and tells you WHICH hop is broken:
#
#   DAQ app --writes--> metrics.prom --served by--> prometheus_exposer.py
#      --network--> Prometheus --stores--> TSDB --reads--> Grafana
#
# Works with the stack down too - in that case it does the host-side network
# checks only, which is exactly what you want before the first ./on.sh.
##############################################################################
set -uo pipefail
cd "$(dirname "$0")"

WATCH=0
[ "${1:-}" = "--watch" ] && WATCH=1

bold=$(tput bold 2>/dev/null || true); red=$(tput setaf 1 2>/dev/null || true)
grn=$(tput setaf 2 2>/dev/null || true); ylw=$(tput setaf 3 2>/dev/null || true)
rst=$(tput sgr0 2>/dev/null || true)
step() { echo; echo "${bold}== $* ==${rst}"; }
ok()   { echo "  ${grn}OK${rst}    $*"; }
warn() { echo "  ${ylw}WARN${rst}  $*"; }
bad()  { echo "  ${red}FAIL${rst}  $*"; }
hint() { echo "        -> $*"; }

run_checks() {
##############################################################################
step "0  Configuration"
##############################################################################
if [ ! -f .env ]; then
    bad "no .env in $(pwd)"
    hint "cp .env.example .env, then edit NETWORK BLOCK 1"
    return 1
fi
set -a; . ./.env; set +a
echo "  targets      : ${DAQ_EXPORTER_TARGETS:-<unset>}"
echo "  scrape       : every ${DAQ_SCRAPE_INTERVAL:-?} (timeout ${DAQ_SCRAPE_TIMEOUT:-?})"
echo "  tunnel       : ${USE_TUNNEL:-no}"
echo "  local ports  : prometheus ${PROMETHEUS_PORT:-9092}, grafana ${GRAFANA_PORT:-3002} on ${WEB_BIND_ADDR:-0.0.0.0}"
if [ ! -f prometheus/prometheus.yml ]; then
    warn "prometheus/prometheus.yml not rendered yet"; hint "./render.sh"
elif [ .env -nt prometheus/prometheus.yml ]; then
    warn ".env has been edited since the config was rendered - Prometheus is still using the OLD target"
    hint "./render.sh && ./reload.sh"
else
    ok "prometheus.yml is up to date with .env"
fi

##############################################################################
step "1  Can THIS machine reach the DAQ exposer?"
##############################################################################
# Split "host:port" without a subshell so the loop can set the outer counters.
# ${arr[@]+...} keeps bash 4.2 (RHEL/CC7) from treating an empty array as unbound.
IFS=',' read -r -a _targets <<< "${DAQ_EXPORTER_TARGETS:-}"
for t in ${_targets[@]+"${_targets[@]}"}; do
    t="${t#"${t%%[![:space:]]*}"}"; t="${t%"${t##*[![:space:]]}"}"
    [ -n "$t" ] || continue
    host="${t%:*}"; port="${t##*:}"
    echo
    echo "  target ${bold}${t}${rst}"

    case "$host" in
        daq-tunnel)
            warn "reached through the ssh sidecar, not from this shell - see section 2"
            continue ;;
        host.docker.internal)
            host=127.0.0.1
            echo "        (resolves to this host from inside the containers; probing 127.0.0.1:$port)" ;;
    esac

    # --- DNS -------------------------------------------------------------
    if [[ "$host" =~ ^[0-9.]+$ ]]; then
        ip="$host"; ok "literal IP $ip (no DNS involved)"
    elif ip="$(getent hosts "$host" 2>/dev/null | awk '{print $1; exit}')" && [ -n "$ip" ]; then
        ok "DNS: $host -> $ip"
    else
        bad "DNS: cannot resolve '$host'"
        hint "wrong name, or this machine has no DNS for the beam-line subnet."
        hint "Put the literal IP in DAQ_EXPORTER_TARGETS, or add a line to /etc/hosts."
        continue
    fi

    # --- TCP -------------------------------------------------------------
    # Bare TCP first: it separates "firewall/nothing listening" from "HTTP is wrong".
    if timeout 5 bash -c "exec 3<>/dev/tcp/$ip/$port" 2>/dev/null; then
        ok "TCP: $ip:$port accepts connections"
    else
        bad "TCP: nothing accepting on $ip:$port"
        hint "On the DAQ PC: is prometheus_exposer.py running?  ss -lntp | grep $port"
        hint "To be scraped directly it must bind 0.0.0.0, not 127.0.0.1."
        hint "Bound to localhost on purpose (:$port has no auth)? That is the tunnel case:"
        hint "set USE_TUNNEL=yes in .env and DAQ_EXPORTER_TARGETS=daq-tunnel:$port"
        continue
    fi

    # --- HTTP + content --------------------------------------------------
    body="$(curl -sf --max-time "${CHECK_HTTP_TIMEOUT:-8}" "http://$ip:$port/metrics" 2>/dev/null)"
    if [ -z "$body" ]; then
        bad "HTTP: GET http://$ip:$port/metrics returned nothing"
        hint "Something is listening on that port but it is not the exposer."
        continue
    fi
    ok "HTTP: /metrics served, $(printf '%s' "$body" | wc -l) lines"

    present="$(printf '%s\n' "$body" | awk '$1=="tkfe_exposer_file_present"{print $2; exit}')"
    if [ "$present" = "1" ]; then
        ok "the exposer is reading a real .prom file"
    else
        bad "tkfe_exposer_file_present=0 - the exposer is up but the .prom file is not there"
        hint "The DAQ writes it as <run dir>/metrics.prom (or --metrics-file <path>)."
        hint "Start the exposer on the SAME path, or start a run."
        continue
    fi

    spills="$(printf '%s\n' "$body" | awk '$1=="tkfe_daq_spills_total"{print $2; exit}')"
    stamp="$(printf '%s\n' "$body" | awk '$1=="tkfe_daq_last_update_timestamp_seconds"{print $2; exit}')"
    if [ -n "$stamp" ]; then
        age=$(( $(date +%s) - ${stamp%.*} ))
        if   [ "$age" -lt 120 ];  then ok   "DAQ is live: ${spills:-0} spills, last write ${age}s ago"
        elif [ "$age" -lt 1800 ]; then warn "DAQ quiet: ${spills:-0} spills, last write ${age}s ago (between runs?)"
        else                           bad  "DAQ stale: last write ${age}s ago - the run is over or the process is wedged"
        fi
    else
        warn "no tkfe_daq_* metrics yet - exposer is fine, the DAQ has not written a spill"
    fi
done

##############################################################################
step "2  SSH tunnel"
##############################################################################
if [ "${USE_TUNNEL:-no}" != "yes" ]; then
    echo "  not in use (USE_TUNNEL=no) - skipped"
else
    if [ -f tunnel/id_ed25519 ]; then
        perm="$(stat -c %a tunnel/id_ed25519)"
        [ "$perm" = "600" ] && ok "key tunnel/id_ed25519 present (mode 600)" \
            || { warn "key mode is $perm, ssh will refuse it"; hint "chmod 600 tunnel/id_ed25519"; }
    else
        bad "tunnel/id_ed25519 missing"
        hint "ssh-keygen -t ed25519 -N '' -f tunnel/id_ed25519"
        hint "ssh-copy-id -i tunnel/id_ed25519.pub ${TUNNEL_SSH_TARGET:-user@daq-pc}"
    fi
    if [ -f tunnel/id_ed25519 ]; then
        # BatchMode: never sit at a password prompt inside a diagnostic.
        if ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
               -o IdentitiesOnly=yes -i tunnel/id_ed25519 ${TUNNEL_SSH_EXTRA_OPTS:-} \
               "${TUNNEL_SSH_TARGET:-}" true 2>/dev/null; then
            ok "ssh to ${TUNNEL_SSH_TARGET} works with that key"
        else
            bad "ssh to ${TUNNEL_SSH_TARGET} failed with that key"
            hint "run it by hand to see why: ssh -i tunnel/id_ed25519 ${TUNNEL_SSH_EXTRA_OPTS:-} ${TUNNEL_SSH_TARGET}"
            hint "needs a PASSWORDLESS key whose .pub is in authorized_keys on that host"
        fi
    fi
    if docker ps --filter 'name=tkfe-daq-tunnel' --format '{{.Names}}' 2>/dev/null | grep -q .; then
        ok "sidecar container tkfe-daq-tunnel is running"
    else
        warn "sidecar not running"; hint "./on.sh  (it starts it when USE_TUNNEL=yes)"
    fi
fi

##############################################################################
step "3  Prometheus - is it scraping, from its own point of view?"
##############################################################################
# This is the authoritative check: Prometheus scrapes from inside the container
# network, which can succeed or fail differently from the host-side probe above.
purl="http://localhost:${PROMETHEUS_PORT:-9092}"
if ! curl -sf --max-time 5 "$purl/-/ready" >/dev/null 2>&1; then
    warn "Prometheus is not answering on $purl"
    hint "not started yet? -> ./on.sh     started but unhealthy? -> docker compose logs prometheus"
else
    ok "Prometheus is up at $purl"
    if command -v jq >/dev/null; then
        curl -sf --max-time 5 "$purl/api/v1/targets?state=active" \
        | jq -r '.data.activeTargets[] | select(.labels.job=="tkfe_daq")
                 | [.scrapeUrl, .health, (.lastError // "")] | @tsv' 2>/dev/null \
        | while IFS=$'\t' read -r url health err; do
            if [ "$health" = "up" ]; then
                ok "scrape $url -> up"
            else
                bad "scrape $url -> $health"
                [ -n "$err" ] && hint "Prometheus says: $err"
                hint "This is the view from INSIDE the container. 'connection refused' to a"
                hint "127.0.0.1 address means you want host.docker.internal, not localhost."
            fi
        done
        n="$(curl -sf --max-time 5 "$purl/api/v1/query?query=count(up%7Bjob%3D%22tkfe_daq%22%7D)" | jq -r '.data.result[0].value[1] // "0"')"
        [ "${n:-0}" = "0" ] && { bad "no tkfe_daq targets at all"; hint "./render.sh && ./reload.sh"; }
    else
        warn "jq not installed, skipping the per-target report"; hint "./install.sh"
    fi

    # --- is data actually LANDING in the TSDB? ---------------------------
    q() { curl -sf --max-time 5 -G "$purl/api/v1/query" --data-urlencode "query=$1" 2>/dev/null \
          | sed -n 's/.*"value":\[[^,]*,"\([^"]*\)".*/\1/p'; }
    series="$(q 'count(tkfe_daq_spills_total)')"
    if [ -n "$series" ] && [ "${series%.*}" -gt 0 ] 2>/dev/null; then
        ok "TSDB has DAQ data: $series series of tkfe_daq_spills_total"
        head="$(q 'prometheus_tsdb_head_series')"
        span="$(q 'time() - (prometheus_tsdb_lowest_timestamp_seconds > 0)')"
        [ -n "$head" ] && echo "        head series: ${head%.*}   retention window: ${PROM_RETENTION:-7d}"
        [ -n "$span" ] && echo "        oldest sample: $(( ${span%.*} / 3600 ))h old"
    else
        warn "no tkfe_daq_* samples stored yet"
        hint "if section 1 passed, just wait one scrape interval (${DAQ_SCRAPE_INTERVAL:-5s})"
    fi
fi

##############################################################################
step "4  Grafana"
##############################################################################
gurl="http://localhost:${GRAFANA_PORT:-3002}"
if curl -sf --max-time 5 "$gurl/api/health" >/dev/null 2>&1; then
    ok "Grafana is up at $gurl"
    echo "        dashboard: $gurl/d/tkfe-daq"
else
    warn "Grafana is not answering on $gurl"; hint "./on.sh, then: docker compose logs grafana"
fi
echo
}

if [ "$WATCH" -eq 1 ]; then
    while true; do clear; date; run_checks; echo; echo "(--watch: refreshing in 5s, Ctrl-C to stop)"; sleep 5; done
else
    run_checks
fi
