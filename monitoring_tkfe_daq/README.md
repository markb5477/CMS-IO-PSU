# tk-fe-daq test-beam monitoring — Prometheus + Grafana

Scrapes the DAQ metrics that `tk-fe-daq` now exports, keeps **one week** of
history, and draws them in Grafana.

Self-contained: this folder is the whole deployment. Copy it to whichever
machine will run the monitoring, and follow the four commands below.

---

## Quick start (monitoring PC)

```bash
./install.sh --pull          # 1. docker, compose, curl, jq, envsubst + cache the images
cp .env.example .env         # 2. one config file...
$EDITOR .env                 #    ...edit NETWORK BLOCK 1: where the DAQ exposer is
./check.sh                   # 3. is that address actually reachable from here?
./render.sh && ./on.sh       # 4. go
```

Then open **Grafana** at `http://<this-pc>:3002` → folder *TK FE DAQ* →
**tk-fe-daq – test beam**. Prometheus is at `http://<this-pc>:9092`.

## Quick start (DAQ PC)

The metrics come out of the DAQ as a text file that has to be served over HTTP.
Copy one script over and run it:

```bash
scp daq-host/start-exposer.sh user@daq-pc:~/
ssh user@daq-pc './start-exposer.sh --install-service'   # systemd --user, survives logout
```

and start the DAQ pointing at the **same fixed path** the exposer serves:

```bash
ext_trigger_daq_double_readout_v2 ... --metrics-file /metrics/tkfe_daq_it.prom
```

> Use a fixed path, not the default. By default the DAQ writes
> `<run dir>/metrics.prom`, and every run gets a fresh `run-<timestamp>`
> directory — the exposer would be left serving the previous run's file forever.
> (`tk-fe-daq/metrics_exposer.md` makes the same point.)

> **If the DAQ runs in the board container** — the usual case — the path has to
> exist on both sides. `start-exposer.sh` creates `/metrics` world-writable on
> the host; add the mount to the docker-run line:
> `-v /metrics:/metrics` (podman: `-v /metrics:/metrics:z`, or SELinux blocks it
> silently).

---

## What is being monitored

`ext_trigger_daq_double_readout_v2` rewrites its `.prom` file once per spill
(`common_tools/utils/prometheus_metrics.hpp`, written to a `.tmp` and renamed, so
a scrape never sees a half-written file). `scripts/prometheus_exposer.py` re-reads
that file on every scrape and serves it at `/metrics`.

| Metric | Type | Meaning |
|---|---|---|
| `tkfe_daq_spills_total` | counter | spills completed **since this run started** |
| `tkfe_daq_triggers_total` | counter | triggers accepted, accumulated from the 16-bit FW counter's per-spill deltas (so it survives wraps); stays 0 on FW without the testbeam trigger counter |
| `tkfe_daq_words_total` | counter | data words read (daqpath + per-chip streams) |
| `tkfe_daq_words_last_spill` | gauge | words in the most recent spill |
| `tkfe_daq_readout_seconds` | gauge | wall-clock time of the most recent FIFO drain |
| `tkfe_daq_fifo_full` | gauge | 1 = a FIFO was at its ceiling at readout → **that spill was truncated in firmware** |
| `tkfe_daq_last_update_timestamp_seconds` | gauge | when the DAQ last rewrote the file — this is what turns "DAQ crashed" from invisible into obvious |
| `tkfe_exposer_file_present` | gauge | 1 = the exposer found a real `.prom` file; 0 = it is serving nothing |

Two things worth knowing when reading the dashboard:

* **The counters reset to zero on every new run** (new process, fresh
  `DaqMetrics`). That is why the activity panels use `rate()`, which handles
  counter resets; the *totals* tiles are per-run, not per-beam-period.
* **`up` vs `tkfe_exposer_file_present` vs the timestamp age** split the three
  failure modes apart: network/exposer down, exposer up but no file, file
  present but nobody writing it.

Alert rules for exactly those cases live in `prometheus/alerts.yml` and show up
under *Prometheus → Alerts*. There is deliberately no Alertmanager — one less
thing to keep alive at a beam line.

---

## Storage: one week

`.env` sets it, `docker-compose.yml` passes it to Prometheus:

```
PROM_RETENTION=7d            # samples older than this are dropped
PROM_RETENTION_SIZE=5GB      # whichever limit is hit first wins
```

At 5 s scrape and ~10 series this is a few tens of MB a week, so raising
`PROM_RETENTION` to cover the whole beam period costs almost nothing.

Data lives in the `prometheus-data` **named volume**, not in this folder.
`./off.sh` leaves it alone — the history is hot and immediately queryable again
after `./on.sh`. Only `./off.sh --wipe` deletes it, and it asks first.

To take a copy off the machine:

```bash
docker run --rm -v tkfe-daq-monitoring_prometheus-data:/data:ro -v "$PWD":/out \
    alpine tar czf /out/tkfe-tsdb.tar.gz -C /data .
```

---

## The network, which is the part that will bite

Everything network-related is in **`.env`**, in three labelled blocks, and
**`./check.sh`** tells you which hop is broken. It walks the chain:

```
DAQ app --writes--> metrics.prom --served by--> prometheus_exposer.py
   --network--> Prometheus --stores--> TSDB --reads--> Grafana
```

and prints `OK` / `WARN` / `FAIL` per hop with the fix under it. It works with
the stack down too, so run it *before* the first `./on.sh`. `./check.sh --watch`
re-runs it every 5 s for a control-room screen.

### Three ways to reach the DAQ PC

Pick one in `.env` → `DAQ_EXPORTER_TARGETS`:

| Situation | Setting | Also |
|---|---|---|
| Normal LAN, port reachable | `daq-pc:9110` | exposer must bind `0.0.0.0` (the default) |
| DAQ PC firewalled, or untrusted network | `daq-tunnel:9110` | set `USE_TUNNEL=yes`, put a passwordless key in `tunnel/id_ed25519` |
| Exposer on this same machine | `host.docker.internal:9110` | **not** `localhost` — that is the container itself |
| Several DAQ PCs | `daq-pc-1:9110,daq-pc-2:9110` | they appear as separate series, and in the `DAQ PC` picker |

For the tunnel:

```bash
ssh-keygen -t ed25519 -N '' -f tunnel/id_ed25519
ssh-copy-id -i tunnel/id_ed25519.pub user@daq-pc.cern.ch
# jump host / IPv4-only? -> TUNNEL_SSH_EXTRA_OPTS=-4 -J lxtunnel.cern.ch
```

The tunnel is also the safer option regardless of firewalls: `:9110` is
unauthenticated (`metrics_exposer.md` says so explicitly), and the ssh forward
lands on the DAQ PC's `127.0.0.1:9110`, so the exposer can stay bound to
localhost and nothing is exposed off-box.

### Symptom → cause

| What you see | What it means | Fix |
|---|---|---|
| `check.sh`: DNS cannot resolve | this PC has no DNS for the beam-line subnet | put the literal IP in `DAQ_EXPORTER_TARGETS`, or add `/etc/hosts` |
| `check.sh`: nothing accepting on `host:9110` | exposer not running, bound to `127.0.0.1`, or a firewall | start it with `--bind 0.0.0.0`; open the port (`firewall-cmd`/`ufw` — `start-exposer.sh` prints the exact command) |
| Prometheus target `DOWN`, host-side probe `OK` | the *container* cannot reach it | `localhost` → `host.docker.internal`; on a tunnel setup check `tkfe-daq-tunnel` is running |
| Target flaps up/down | scrape timing out on a slow link | raise `DAQ_SCRAPE_TIMEOUT` (and `DAQ_SCRAPE_INTERVAL`) in `.env`, `./reload.sh` |
| Dashboard empty, everything `OK` | config edited but not re-rendered | `./render.sh && ./reload.sh` |
| `install.sh`: pull failed | no route to Docker Hub | set `https_proxy` (see the header of `install.sh`), then `./install.sh --pull` |
| Grafana slow to load | it is trying to phone home to grafana.com | already disabled in `docker-compose.yml`; if it persists, the proxy is intercepting |

**Before you travel to the beam line**, on a good network:

```bash
./install.sh --pull      # caches the exact images named in .env
```

after which `./on.sh` needs no registry at all. Pin `PROMETHEUS_IMAGE` /
`GRAFANA_IMAGE` to real tags rather than `:latest` while you are at it.

---

## Files

```
.env.example        ALL configuration, three labelled NETWORK blocks   <- edit this
install.sh          dependencies (--check to verify, --pull to cache images)
check.sh            end-to-end network diagnostic (--watch)            <- run this
render.sh           .env  ->  prometheus/prometheus.yml
on.sh / off.sh      start / stop (off.sh keeps the data; --wipe deletes it)
reload.sh           apply an edited target to a RUNNING Prometheus, no restart
docker-compose.yml  prometheus + grafana + optional ssh sidecar
prometheus/         prometheus.yml.tmpl (source), alerts.yml
grafana/            datasource + dashboard, both auto-provisioned
tunnel/             autossh sidecar, only used when USE_TUNNEL=yes
daq-host/           start-exposer.sh — the only file that goes on the DAQ PC
```

## Command reference

| | |
|---|---|
| `./install.sh --check` | verify dependencies, change nothing |
| `./check.sh` | where is the chain broken? |
| `./on.sh` / `./off.sh` | start / stop |
| `./reload.sh` | new target address, no restart, no data loss |
| `docker compose logs -f prometheus` | scrape errors in full |
| `docker compose logs -f daq-tunnel` | ssh sidecar reconnects |

## Ports

This stack uses **9092** (Prometheus) and **3002** (Grafana) so it can run
alongside `../monitoring` (9090/3000) and `../monitoring_beam_test` (9091/3001)
on the same host. Change them in `.env` if something else is already there.
