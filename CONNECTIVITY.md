# Connectivity checklist — is the whole chain alive?

For bring-up after moving the setup. Walks the chain end to end, one hop at a
time, with the command that proves each hop, what a good answer looks like, and
what to do when it isn't.

Work **in order**. Each hop assumes the ones before it are green — a failure at
hop 2 makes every later check meaningless, so don't skip ahead.

```
[1] CPX200DP ──192.168.50.0/24 (USB dongle)──► [2] lab PC  cmsladdertest
                                                    ├─ cpx-exporter  :9820 (loopback only)
                                                    └─ bert-exporter :9821 (loopback only)
                                                          ▲ follows bertContinuous.csv
                                                          │ written by the mm_acf DAQ
                                                          │
                                              [3] SSH out from the monitor PC
                                                  (xtaldaq@, key auth)
                                                          │
[4] monitor PC  prometheus-tk ── psu-tunnel sidecar republishes :9820/:9821
                                  on the docker network as host "psu-tunnel"
                                                          │
                                              [5] Prometheus :9090 scrapes it
                                                          │
                                              [6] Grafana :3000 reads Prometheus
                                                          │
                                              [7] your laptop, over ssh -L
```

Two facts that prevent most false alarms:

- **The exporters bind to loopback (`127.0.0.1`) on purpose.** Curling
  `cmsladdertest:9820` from anywhere else will *always* fail. That is correct,
  not a fault — the only legitimate path in is the SSH tunnel (hop 3/4).
- **A scrape target showing `up` does not mean the instrument is connected.**
  `up` only means the exporter answered HTTP. Whether the PSU is actually on the
  other end is `cpx_up`, and whether the DAQ's CSV is being found is
  `bert_file_present`. Check those separately (hop 5b).

---

## 0. The 60-second verdict

Run this **on the monitor PC** (`ssh prometheus-tk`). If both hops report `up`
and the two health gauges read `1`, the entire chain from PSU to Grafana is good
and you can stop here.

```sh
cd /root/monitoring

# every scrape target and why it's failing, if it is
curl -s localhost:9090/api/v1/targets \
  | jq -r '.data.activeTargets[] | "\(.labels.job)\t\(.health)\t\(.scrapeUrl)\t\(.lastError)"'

# instrument- and file-level health (NOT the same as target health)
curl -s 'localhost:9090/api/v1/query?query=cpx_up'          | jq -r '.data.result[].value[1]'
curl -s 'localhost:9090/api/v1/query?query=bert_up'         | jq -r '.data.result[].value[1]'
curl -s 'localhost:9090/api/v1/query?query=bert_file_present'| jq -r '.data.result[].value[1]'

# anything actively complaining
curl -s localhost:9090/api/v1/alerts \
  | jq -r '.data.alerts[] | "\(.labels.alertname)\t\(.state)"'
```

Good looks like:

```
cpx_psu       up   http://psu-tunnel:9820/metrics
bert_status   up   http://psu-tunnel:9821/metrics
1        <- cpx_up          : exporter is talking to the PSU
1        <- bert_up         : exporter read the CSV
1        <- bert_file_present: it found a bertContinuous.csv
(no alerts)
```

An **empty** answer (blank line, not `0`) from one of the gauge queries means the
series isn't being collected at all — the exporter is down, not that the value is
zero. Treat blank as "go to the hop", same as `0`.

Anything else — find the symptom in the table below and jump to that hop.

| symptom | most likely | go to |
|---|---|---|
| target `down`, `connection refused` | exporter not running, or tunnel down | hop 4, then 2 |
| target `down`, `no such host psu-tunnel` | tunnel container missing/crashed | hop 4 |
| target `up`, but `cpx_up 0` | PSU cable / dongle / PSU mains | hop 1 |
| target `up`, but `bert_file_present 0` | wrong `BERT_RESULTS_ROOT`, or no DAQ run yet | hop 2b |
| target `up`, `bert_up 1`, curves frozen | BER round-robin (usually normal) | hop 5b |
| both targets `down` at once | SSH auth / lab PC unreachable | hop 3 |
| Prometheus fine, Grafana empty | datasource or dashboard provisioning | hop 6 |
| nothing loads from your laptop | ssh -L tunnel | hop 7 |

---

## 1. PSU ◄─► lab PC (the private link)

**On the lab PC.** This link is a USB-ethernet dongle on a private
`192.168.50.0/24`, not the CERN network — the most fragile part of a move.

```sh
ip -br addr | grep -E '192.168.50|169.254'   # dongle must hold BOTH addresses
ping -c2 192.168.50.2                        # the PSU
```

Then prove the exporter is really talking to it:

```sh
curl -s localhost:9820/metrics | grep '^cpx_up'     # cpx_up 1
curl -s localhost:9820/status | head              # live readings, IDN string
```

**`cpx_up 0` or ping fails:**

1. Check the cable between the dongle and the PSU's rear LAN port.
2. Check the PSU is powered and its LAN LEDs are live.
3. `ip -br addr` shows no `192.168.50.1` → the NetworkManager profile didn't
   bind. It's keyed to the dongle MAC (`00:23:57:5c:28:98`), so a *different*
   dongle needs the profile updated:
   `nmcli con modify psu-link 802-3-ethernet.mac-address <new MAC>`, then
   `nmcli con up psu-link`.
4. Ping works but `cpx_up 0` → something else holds the PSU's single SCPI
   socket. Only one connection is allowed. `systemctl --user restart cpx-exporter`.
5. PSU lost its static IP (factory reset drops it to link-local): that is what
   the spare `169.254.0.1/16` on the dongle is for —
   `avahi-resolve -n t527059.local.` or `ip neigh show dev <dongle>` to find it,
   then re-pin `192.168.50.2` from its LXI web page. **LAN changes need a PSU
   power-cycle.**

> Don't test with `nc 192.168.50.2 9221` while the exporter is running — the CPX
> allows one SCPI socket and the exporter owns it, so `nc` just hangs. Watch
> `cpx_up` instead.

---

## 2. The two exporters on the lab PC

**On the lab PC.** Both are per-user systemd services (no sudo), both processes
are called `exporter.py`, and they are told apart by **port**.

```sh
systemctl --user status cpx-exporter bert-exporter --no-pager | head -20
ss -ltnp | grep -E ':(9820|9821)'      # both listening on 127.0.0.1
curl -s localhost:9820/metrics | head -3
curl -s localhost:9821/metrics | head -3
```

Expect both listening on `127.0.0.1:9820` and `127.0.0.1:9821`.

**Not running:**

```sh
cd ~/cpx-psu-monitor && ./on.sh      # PSU
cd ~/bert-monitor    && ./on.sh      # BER
journalctl --user -u cpx-exporter -n 50 --no-pager
journalctl --user -u bert-exporter -n 50 --no-pager
```

**They died when you logged out** — linger isn't enabled. This is the classic
"worked while I was ssh'd in, dead an hour later":

```sh
loginctl enable-linger $USER
loginctl show-user $USER | grep Linger      # Linger=yes
```

### 2b. Is the BER exporter looking at the right CSV?

The single most likely thing to break after a move, because the DAQ's output
directory depends on where it was launched from.

```sh
curl -s localhost:9821/status | grep -m1 path      # the file it locked onto
curl -s localhost:9821/metrics | grep -E '^bert_(file_present|file_rows|active_series)'
```

- `"path": null` / `bert_file_present 0` → it found nothing. Either the DAQ
  hasn't created its `Results/OT_ModuleTest_<id>_Run<n>/` directory yet, or
  `BERT_RESULTS_ROOT` points at the wrong place.
- Wrong file → fix `~/bert-monitor/.env` (`BERT_RESULTS_ROOT` for the
  follow-newest glob, or `BERT_CSV` to pin one exact file), then
  `systemctl --user restart bert-exporter` and re-check `path`.
- A pinned `BERT_CSV` that doesn't exist does **not** fall back to the glob — it
  just reports `bert_file_present 0`. Leave it commented out for normal running.

Config detail: README.md → "Pointing the BER exporter at the CSV".

---

## 3. SSH from the monitor PC to the lab PC

**On the monitor PC.** The tunnel SSHes *out* to the lab PC as `xtaldaq`, using a
key that lives only on the monitor PC. If both targets went down at once, this
is almost always the hop.

```sh
docker logs --tail=30 cpx-psu-tunnel
```

Healthy = quiet (autossh says nothing once connected). Bad, and what it means:

| log line | cause | fix |
|---|---|---|
| `Permission denied (publickey)` | key not authorised on the lab PC | re-add the pubkey, below |
| `Could not resolve hostname` | dyndns hasn't caught up after the move | wait, or use the new IP |
| `Connection timed out` | lab PC down / firewall / wrong host | check the PC is up |
| `remote port forwarding failed` | port already in use on the lab PC | kill the stray listener |

Re-authorise the key (one-time, and after any lab-PC reinstall):

```sh
# on the monitor PC
cat /root/monitoring/tunnel/id_ed25519.pub
# then append that line to ~xtaldaq/.ssh/authorized_keys on the lab PC
```

If the key is missing entirely (fresh monitor PC — it is gitignored and excluded
from the deploy, so it never arrives by rsync):

```sh
cd /root/monitoring
ssh-keygen -t ed25519 -f tunnel/id_ed25519 -N "" -C "psu-tunnel@prometheus"
cat tunnel/id_ed25519.pub        # -> lab PC authorized_keys
docker compose up -d --build psu-tunnel
```

If the lab PC's address changed, update `TUNNEL_SSH_TARGET` in
`/root/monitoring/.env` and `docker compose up -d psu-tunnel`.

---

## 4. The tunnel sidecar ◄─► Prometheus (inside Docker)

**On the monitor PC.** This is the decisive test: it asks Prometheus's own
container to fetch through the tunnel, exactly as a scrape does. If this works,
hops 1–4 are all good.

```sh
docker ps --filter name=cpx- --format '{{.Names}}\t{{.Status}}'

docker exec cpx-prometheus wget -qO- http://psu-tunnel:9820/metrics | head -3
docker exec cpx-prometheus wget -qO- http://psu-tunnel:9821/metrics | head -3
```

Expect three lines of Prometheus text from each (`# HELP cpx_up ...` /
`# HELP bert_up ...`).

- `wget: can't connect` → the forward isn't up: hop 3.
- `bad address 'psu-tunnel'` → the sidecar container isn't running:
  `docker compose up -d psu-tunnel`, then `docker logs cpx-psu-tunnel`.
- Hangs → the lab PC is reachable but the exporter behind the forward is dead:
  hop 2.

Restart just the tunnel without disturbing the database:

```sh
cd /root/monitoring && docker compose restart psu-tunnel
```

---

## 5. Prometheus

**On the monitor PC.** Confirm it is scraping what you think it is.

```sh
grep -E 'job_name|targets|scrape_interval' /root/monitoring/prometheus/prometheus.yml
```

Expect `cpx_psu` → `psu-tunnel:9820` @4s and `bert_status` → `psu-tunnel:9821` @2s.

**`prometheus.yml` is generated** from `prometheus.yml.tmpl` by `render.sh`, and
it is gitignored — so after editing `.env` it is stale until you re-render:

```sh
cd /root/monitoring
./render.sh                                   # regenerate from .env
curl -X POST localhost:9090/-/reload          # hot-reload, no data loss
```

If `bert_status` is missing from the file entirely, `.env` lacks
`BERT_EXPORTER_TARGET` — add it, `./render.sh`, reload.

### 5b. Health beyond "the target is up"

```sh
# the instrument itself
curl -s 'localhost:9090/api/v1/query?query=cpx_up' | jq -r '.data.result[].value[1]'

# BER: file found, and how stale the newest sample is (seconds)
curl -s 'localhost:9090/api/v1/query?query=bert_file_present' | jq -r '.data.result[].value[1]'
curl -s 'localhost:9090/api/v1/query?query=time()-bert_file_mtime_seconds' | jq -r '.data.result[].value[1]'
```

**Frozen BER curves are usually correct.** The DAQ round-robins across lines and
caches a phase scan per line, so a given link updates only once per full cycle
(~1 h at the GIPHT config). Judge liveness by the **file**
(`time() - bert_file_mtime_seconds`, should stay small), not by a single link.

---

## 6. Grafana

**On the monitor PC.**

```sh
curl -s localhost:3000/api/health                       # {"database":"ok",...}

# datasource + dashboards actually provisioned (password from .env)
curl -su "admin:$GRAFANA_ADMIN_PASSWORD" localhost:3000/api/datasources | jq -r '.[].name,.[].url'
curl -su "admin:$GRAFANA_ADMIN_PASSWORD" localhost:3000/api/search?type=dash-db | jq -r '.[].title'
```

Expect the `Prometheus` datasource at `http://prometheus:9090`, and both
dashboards: *CPX200DP Power Supply* and *OT Module BER*.

- Dashboards missing → provisioning didn't mount. `docker compose restart grafana`,
  then `docker logs cpx-grafana | grep -i provision`.
- Panels say "No data" but Prometheus has the data → the datasource UID drifted.
  The dashboards hard-code `cpx-prometheus`; confirm it matches
  `grafana/provisioning/datasources/datasource.yml`.
- "No data" only on *stat* panels while graphs are full → you are viewing a past
  window whose end is after the exporter stopped. Stat panels use instant
  queries. Set the range to `now`, or a window while it was running.

---

## 7. From your laptop

Ports 3000/9090 sit behind the CERN firewall, so tunnel them (details in
GUIDE.md §4):

```sh
ssh -N -L 3000:localhost:3000 -L 9090:localhost:9090 root@cmx-trk-prometheus-instance
# then http://localhost:3000
```

`channel N: open failed: connect failed: Connection refused` means the tunnel is
fine but the stack is down on the host — `cd /root/monitoring && ./on.sh`.

---

## After a move: what to re-check, and why

Not everything breaks equally. Ordered by how often it bites:

| what changed | what breaks | re-check |
|---|---|---|
| PSU moved / re-cabled | private link | hop 1 — cable, dongle addresses, PSU static IP survived the power-cycle |
| PSU factory-reset | static IP lost | hop 1 — recover via `169.254.x`, re-pin, power-cycle |
| Different USB-ethernet dongle | NM profile MAC | hop 1 — `nmcli con modify psu-link 802-3-ethernet.mac-address <MAC>` |
| Lab PC moved rooms | its IP, via dyndns | hop 3 — hostname resolves; `TUNNEL_SSH_TARGET` still right |
| Lab PC reinstalled / new account | SSH key, services, linger | hops 2 + 3 — re-add pubkey, re-enable user services, `enable-linger` |
| DAQ launched from a new directory | CSV discovery | hop 2b — `BERT_RESULTS_ROOT` |
| Monitor PC rebuilt | tunnel key, rendered config | hops 3 + 5 — regenerate key, `./render.sh` |
| `.env` edited anywhere | stale generated config | hop 5 — `./render.sh` + reload; restart the affected exporter |

A full cold bring-up, in dependency order:

```sh
# 1. lab PC
cd ~/cpx-psu-monitor && ./on.sh
cd ~/bert-monitor    && ./on.sh
curl -s localhost:9820/metrics | grep '^cpx_up'        # 1
curl -s localhost:9821/status  | grep -m1 path         # the CSV

# 2. monitor PC
cd /root/monitoring && ./render.sh && ./on.sh

# 3. verify (hop 0)
curl -s localhost:9090/api/v1/targets \
  | jq -r '.data.activeTargets[] | "\(.labels.job)\t\(.health)\t\(.lastError)"'
```

Then open Grafana and confirm both dashboards draw.

## What the alerts will tell you on their own

Once the stack is up these fire by themselves — a second opinion when you are not
watching:

| alert | after | means |
|---|---|---|
| `CPXExporterDown` | 2m | scrape path broken (hops 2–4) |
| `CPXInstrumentUnreachable` | 2m | exporter alive, PSU not (hop 1) |
| `BERTExporterDown` | 2m | BER scrape path broken |
| `BERTNoFile` | 5m | running, but no `bertContinuous.csv` found (hop 2b) |
| `BERTLoggingStalled` | >5m stale | the continuous BER test stopped or hung |
| `CPXOutputTripped` | instant | OVP/OCP trip |

Note `BERTLoggingStalled` keys on the **newest sample across all links**, so a
startup phase scan (minutes with no new row) can trip it before the run settles.
Treat one at bring-up as informational; if it persists, check hop 2b.
