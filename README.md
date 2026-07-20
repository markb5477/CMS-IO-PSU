# CMS-IO-PSU

Monitoring and control for the Aim-TTi CPX200DP power supply in 186/B-04.

```
 CPX200DP <---- cmsladdertest ----> Prometheus (OpenStack) --> Grafana
 (TCP 9221)     exporter.py :9820   ^   scrapes /metrics
                    ^               |
                    | localhost     '-- psu-tunnel sidecar: Prometheus SSHes OUT
                 psuctl / experiments    to the lab PC and scrapes the forwarded
                                         ports (:9820 direct is firewalled)
 bertContinuous.csv --> cmsladdertest ----> (same Prometheus, job bert_status)
 (written by mm_acf)    exporter.py :9821    scrapes /metrics via the same tunnel
```

Two exporters, same pattern, one monitoring stack:

- **PSU** (`psu-server/exporter.py`, :9820) holds the one TCP connection to the
  CPX200DP, polls it, and serves `/metrics`, `/status`, `/control` (psuctl) and
  `/scpi` (experiments). The last two are localhost-only.
- **BER** (`bert-server/exporter.py`, :9821) has no instrument. It **follows a
  CSV file** the mm_acf DAQ appends to (`bertContinuous.csv`) and serves the
  latest row per link as `/metrics` + `/status`. Read-only; it never writes to
  or interferes with the DAQ's file.

Layout by where it runs:

- `psu-server/` runs on cmsladdertest, next to the PSU:
  - `exporter.py`, the daemon
  - `psuctl`, control CLI (on/off, set V/I/OVP/OCP, trip reset, local)
  - `experiments/`, pymeasure `Procedure`s (ramps, IV sweeps), in a venv
  - `config.py` + `.env`, shared config (host, ports, limits); copy `.env.example`
  - `systemd/`, service unit
- `bert-server/` also runs on cmsladdertest, next to the DAQ's output:
  - `exporter.py`, the CSV-following daemon
  - `config.py` + `.env`, **which CSV to follow** (see "Pointing the BER exporter
    at the CSV" below); copy `.env.example`
  - `systemd/`, service unit + `tests/`, parser tests and a fake CSV feed
- `monitoring/` runs on the OpenStack host: `prometheus/` (scrape + alerts) and `grafana/`
- `deploy-psu-server.sh` rsyncs **both** lab-PC exporters: `psu-server/` to
  `~/cpx-psu-monitor/` and `bert-server/` to `~/bert-monitor/`
- `deploy-monitoring.sh` renders the scrape config, rsyncs `monitoring/` to `prometheus-tk:/root/monitoring/`

Python 3 stdlib only, except the experiments. Nothing to install on the lab PC.

Moving the setup, or something stopped arriving in Grafana? **CONNECTIVITY.md**
walks the whole chain hop by hop (PSU → lab PC → SSH tunnel → Prometheus →
Grafana) with the command that proves each link and how to fix it.

## PSU on the network

CERN won't route to the PSU. An unregistered MAC lands in a quarantine VLAN
(`172.20.x`) the PC can't reach, and the USB serial port wedges on re-plug. So
the PSU gets a point-to-point link: its rear LAN port straight into a
USB-ethernet adapter, leaving the PC's built-in NIC for Prometheus.

Fixed address on each end. On the PSU, set a static IP from its LXI web page
(Configure) or the front panel (`192.168.50.2`, mask `255.255.255.0`) and untick
DHCP and AUTO-IP. LAN changes need a power-cycle. On the PC, a NetworkManager
profile keyed to the adapter's MAC so it survives a different USB port:

```sh
nmcli con add type ethernet con-name psu-link ifname '*' \
    802-3-ethernet.mac-address 00:23:57:5c:28:98 \
    ipv4.method manual ipv4.addresses "192.168.50.1/24,169.254.0.1/16" \
    ipv4.never-default yes ipv6.method ignore
```

The extra `169.254.0.1/16` is a fallback. After a factory reset the PSU drops to
a link-local address (mDNS `t527059.local`), and that's how you get back to it.
Check comms with `echo "*IDN?" | nc 192.168.50.2 9221`, expect `THURLBY
THANDAR, CPX200DP, ...`. Stop the exporter first; it holds the one SCPI socket.
Full moving-the-rig checklist in GUIDE.md.

## Lab PC

`./deploy-psu-server.sh` rsyncs `psu-server/` to `~/cpx-psu-monitor/`. Set config
once (exporter, psuctl and experiments all read this `.env`):

```sh
cd ~/cpx-psu-monitor
cp .env.example .env
$EDITOR .env          # PSU_HOST, ports, MAX_VOLTAGE/MAX_CURRENT
```

Read-only check first. Safe on the live PSU, touches no setpoint or output:

```sh
python3 tests/readonly.py --direct     # straight to the PSU (exporter stopped)
python3 tests/readonly.py              # or via a running exporter
python3 tests/write_check.py           # writes tiny setpoints, verifies, restores
```

Foreground test (flags override `.env` for one-offs):

```sh
python3 exporter.py --max-voltage 30 --max-current 5
curl localhost:9820/metrics
./psuctl status
```

Install as a service (sudo):

```sh
sudo cp -r ~/cpx-psu-monitor /opt/cpx-psu-monitor
sudo cp /opt/cpx-psu-monitor/systemd/cpx-exporter.service /etc/systemd/system/
sudo systemctl enable --now cpx-exporter
```

It reads `/opt/cpx-psu-monitor/.env`; edit and `sudo systemctl restart cpx-exporter`.

Without sudo (user service, survives logout):

```sh
mkdir -p ~/.config/systemd/user
sed -e 's|/opt/cpx-psu-monitor|%h/cpx-psu-monitor|' \
    -e 's|multi-user.target|default.target|' \
    ~/cpx-psu-monitor/systemd/cpx-exporter.service > ~/.config/systemd/user/cpx-exporter.service
systemctl --user enable --now cpx-exporter
loginctl enable-linger $USER
```

No firewall opening is needed for 9820: the CERN firewall blocks a direct scrape
and that is not expected to change, so Prometheus reaches both exporters by
SSHing *out* to this PC through the `psu-tunnel` sidecar — see "Scrape path
(reverse tunnel)" below. All the lab PC needs is inbound SSH for `xtaldaq`.

Monitoring on/off (the exporter is the user service above; PSU outputs are never
touched by start/stop):

```sh
cd ~/cpx-psu-monitor
./on.sh                 # start cpx-exporter + show status
./off.sh                # stop cpx-exporter
systemctl --user start|stop|restart|status cpx-exporter
journalctl --user -u cpx-exporter -f
```

### BER exporter (lab PC, second exporter)

The mm_acf continuous BER test is surfaced by a sibling exporter on :9821. It
takes no instrument connection — it **follows the CSV the DAQ writes**. Full
detail in `bert-server/README.md`; the deploy and CSV wiring are below.

It ships with the PSU exporter — `./deploy-psu-server.sh` rsyncs `bert-server/`
to `~/bert-monitor/` in the same run (both exporters live on cmsladdertest), so
there is nothing extra to copy:

```sh
./deploy-psu-server.sh                    # both: cpx-psu-monitor/ + bert-monitor/
```

Then on the lab PC:

```sh
cd ~/bert-monitor
cp .env.example .env
$EDITOR .env            # <- the CSV config lives here, see next section
./on.sh                 # start bert-exporter (follows bertContinuous.csv)
./off.sh                # stop it (reaps strays by port, never touches cpx-exporter)
```

Both exporters are named `exporter.py`; `on.sh`/`off.sh` key on their metrics
port (9820 vs 9821), so starting/stopping one never affects the other.

### Pointing the BER exporter at the CSV

**Where the config goes: `bert-server/.env` on the lab PC** (i.e.
`~/bert-monitor/.env` after deploying; copy it from `.env.example`). That one
file is the only place the CSV path is configured — `config.py` reads it, and
real environment variables (a systemd `Environment=`, or a one-off
`BERT_CSV=... ./exporter.py`) override it. It is gitignored, so it stays on the
machine and survives re-deploys.

The DAQ writes to a **run-specific** directory:

```
<cwd or $GIPHT_RESULT_FOLDER>/Results/OT_ModuleTest_<ModuleId>_Run<N>/bertContinuous.csv
```

The directory changes every run but the basename never does, so there are two
ways to point at it — pick one:

| mode | set in `.env` | use when |
|---|---|---|
| **glob (default)** | `BERT_RESULTS_ROOT=/home/xtaldaq/mm_acf`<br>`BERT_GLOB=**/bertContinuous.csv` | normal operation — follows the **newest** match and switches automatically when a new run starts |
| **pinned** | `BERT_CSV=/home/xtaldaq/mm_acf/Results/OT_ModuleTest_M123_Run7/bertContinuous.csv` | you want exactly one file (debugging, replaying an old run) |

`BERT_CSV` **wins** over the glob whenever it is set to a non-empty value — and
it does *not* fall back: if that exact path doesn't exist the exporter reports
`bert_file_present 0` rather than globbing. Leave `BERT_CSV` commented out for
normal running. Set
`BERT_RESULTS_ROOT` to wherever the DAQ is launched from — its `Results/` lives
under the working directory unless `$GIPHT_RESULT_FOLDER` /
`$OTSDAQ_RESULTS_FOLDER` points elsewhere.

The rest of `.env` (all optional, defaults are fine):

```sh
BERT_LISTEN=127.0.0.1     # loopback-only; Prometheus reaches it via the tunnel
BERT_HTTP_PORT=9821
BERT_POLL_INTERVAL=2      # how often to re-read the CSV [s], matches the scrape
BERT_ERROR_SENTINEL=4294967295   # errorCount 0xFFFFFFFF -> NaN (negative disables)
BERT_MAX_TESTED_BITS=1e15        # implausible testedBits -> NaN (<=0 disables)
```

Check it found the right file — `/status` reports the path it is actually
following, which is the fastest way to catch a wrong `BERT_RESULTS_ROOT`:

```sh
curl -s localhost:9821/status | head -20      # "path": ".../bertContinuous.csv"
curl -s localhost:9821/metrics | grep -E '^bert_(up|file_present|file_rows|active_series)'
```

`bert_file_present 0` means the glob matched nothing (wrong root, or the DAQ
hasn't started a run yet). `bert_up 1` with `bert_file_rows` climbing means it is
reading new rows.

**Gaps are expected, not a fault.** The DAQ caches a phase scan per line and
round-robins across lines, so any single link updates only once per full cycle
(`numberOfLines × SamplesPerLine × LogIntervalSeconds`, ~1 h at the GIPHT
config). The file-level write cadence — `bert_file_mtime_seconds` — is the real
liveness signal; a flat per-link curve between updates is correct. Prometheus
samples the exporter's latest-value snapshot every 2 s regardless, so the curve
is a step function, not an event stream.

CSV rows that carry hardware read-failure sentinels are exported as `NaN`
(a gap in Grafana) rather than a bogus spike; `bert_invalid_samples_total` and
`bert_parse_errors_total` count them.

## Control

```sh
psuctl status
psuctl set 1 -v 5.0 -i 0.5        # 5 V, 0.5 A limit on channel 1
psuctl set 1 --ovp 6.0 --ocp 1.0
psuctl on 1                       # on/off  1 | 2 | all
psuctl reset                      # clear an OVP/OCP trip
psuctl local --pause 120          # unlock the front panel for 2 min
```

See MANAGE.md for the same actions over raw curl.

- A remote command locks the front panel. `psuctl local` hands it back and
  pauses polling for `--pause` seconds (else the next poll re-locks it).
- `/control` is localhost-only, so control means ssh access. Metrics are open.
- Settings persist in the instrument; the exporter going down leaves outputs untouched.
- **Setpoint guards are currently DISABLED.** The limits in `exporter.py`
  (`HW_MAX_VOLTAGE`/`HW_MAX_CURRENT`/`HW_MAX_POWER`, the OVP/OCP ranges and the
  PowerFlex envelope) and the `MAX_VOLTAGE`/`MAX_CURRENT` ceilings in `.env` were
  all raised to `100000`, so the software rejects nothing. The CPX200DP still
  enforces its own physical limits at the SCPI level. To re-enable protection,
  restore the datasheet values kept in the comment above those constants
  (0-60 V, 0-10 A, 180 W envelope, OVP 1-66 V, OCP 0.01-11 A) and set `.env` back
  to the load's tolerance. The mechanism is unchanged: over-limit commands are
  rejected, not clamped, and the `.env` ceilings can only tighten the code limits.

## Experiments

pymeasure `Procedure`s run in a venv on cmsladdertest and drive the PSU through
the exporter. `shutdown()` runs even on abort, so outputs are always left off.

```sh
python3 -m venv ~/cpx-venv
~/cpx-venv/bin/pip install -r ~/cpx-psu-monitor/experiments/requirements.txt
cd ~/cpx-psu-monitor/experiments
~/cpx-venv/bin/python ramp.py --channel 1 --current-limit 0.5 \
    --v-start 0 --v-stop 5 --v-step 0.5 --dwell 2 --ovp 6 --out sweep.csv
```

For bench bring-up, `--mode direct` talks straight to the PSU, at the
`PSU_HOST`/`PSU_PORT` from `.env` (needs `pip install pyvisa-py`; stop the
exporter first, it holds the one SCPI socket). Subclass `Procedure`
to write your own, see `ramp.py`. GUIDE.md walks through both a manual and an
automated experiment.

## Prometheus + Grafana (OpenStack)

Everything for the monitoring host is under `monitoring/`, run as a Docker stack
(Prometheus + Grafana). `deploy-monitoring.sh` renders the config from `.env`
and rsyncs to `prometheus-tk:/root/monitoring/`. Scrape job, alerts, datasource
and dashboard are all wired in, nothing to import by hand.

From your laptop, set the target once and deploy:

```sh
cd monitoring
cp .env.example .env            # PSU_EXPORTER_TARGET, interval/labels, ports, GRAFANA_ADMIN_PASSWORD
cd .. && ./deploy-monitoring.sh
```

On the box (`ssh prometheus-tk`), turn the stack on/off (needs Docker + compose):

```sh
cd /root/monitoring
cp .env.example .env    # first time: GRAFANA_ADMIN_PASSWORD / ports
./on.sh                 # up -d  (Prometheus :9090, Grafana :3000)
./off.sh                # down, KEEPS data hot in the volume for next start
./off.sh --wipe         # down AND reset the volume (archived first)
```

`off.sh` never deletes data — the TSDB stays in its named volume, hot and
queryable again the moment you `on.sh`. Both `on.sh` and `off.sh` also drop a
backup tarball in `./storage/` (`archive-tsdb.sh`) as insurance; overlapping
on/off archives can duplicate data, which is fine. `--wipe` is the only path that
resets the volume, and it archives first, so no data is lost. Prometheus is one
TSDB, so every archive holds all series — PSU (`cpx_*`) and BER (`bert_*`) alike.

`render.sh` (run by the deploy) writes `prometheus/prometheus.yml` from
`prometheus.yml.tmpl`. Edit the template, not the output.

**There are two `.env` files and they do different jobs.** `.env` is excluded
from the deploy rsync, so each machine keeps its own:

| where | what it feeds | when it is read |
|---|---|---|
| **your laptop**, `monitoring/.env` | `PSU_EXPORTER_TARGET`, `BERT_EXPORTER_TARGET`, scrape intervals, `INSTRUMENT`/`LOCATION` labels | at deploy time, by `render.sh` — baked into `prometheus.yml` |
| **the box**, `/root/monitoring/.env` | `PROMETHEUS_PORT`, `GRAFANA_PORT`, `GRAFANA_ADMIN_PASSWORD` | at `./on.sh` time, by docker-compose |

Consequence worth knowing: editing a scrape target in the box's `.env` changes
nothing on its own — `prometheus.yml` was already rendered on the laptop. Either
re-run `./deploy-monitoring.sh` from the laptop (normal path), or run
`./render.sh && ./on.sh` on the box to re-render from its own `.env`. The
scrape keys are present in both copies because they share one `.env.example`;
on the box they are simply inert until you run `render.sh` there.

Browserless view: `dash.py` renders a live terminal dashboard from the
Prometheus API (stdlib), run on the box:

```sh
python3 /root/monitoring/dash.py --watch      # live, Ctrl-C to quit
python3 /root/monitoring/dash.py              # one-shot
```

### Scrape path (reverse tunnel)

A direct scrape of `cmsladdertest:9820` is blocked by the CERN firewall, so the
stack includes a `psu-tunnel` sidecar (`tunnel/`, autossh): it SSHes *out* to
`xtaldaq@cmsladdertest`, local-forwards the exporter, and Prometheus scrapes
`psu-tunnel:9820` over the Docker network (`PSU_EXPORTER_TARGET` in `.env`).
One-time key setup on the box:

```sh
cd /root/monitoring
ssh-keygen -t ed25519 -f tunnel/id_ed25519 -N "" -C "psu-tunnel@prometheus"
cat tunnel/id_ed25519.pub      # append to ~xtaldaq/.ssh/authorized_keys on the lab PC
```

The key lives only on the box (gitignored, excluded from the deploy rsync). If
9820 is ever opened through the firewall, set
`PSU_EXPORTER_TARGET=cmsladdertest.dyndns.cern.ch:9820` and drop the sidecar.

## Metrics

`cpx_up`, `cpx_info{idn}`, `cpx_set_voltage_volts`/`cpx_set_current_amps`,
`cpx_output_voltage_volts`/`cpx_output_current_amps`, `cpx_output_power_watts`,
`cpx_output_enabled`, `cpx_constant_voltage`/`cpx_constant_current`,
`cpx_trip_overvoltage`/`cpx_trip_overcurrent`, `cpx_power_limit`,
`cpx_limit_status_register`, `cpx_max_voltage_volts`/`cpx_max_current_amps`,
`cpx_poll_errors_total`, `cpx_poll_duration_seconds`, `cpx_polling_paused`.
Per-channel metrics carry a `channel` label. Poll interval 1 s (meters at 4 Hz).

From the BER exporter (job `bert_status`, scraped every 2 s):

`bert_up`, `bert_file_present`, `bert_info{path}` (which CSV is being followed),
`bert_last_sample_timestamp_seconds` (`time() - this` = staleness),
`bert_file_mtime_seconds`/`bert_file_size_bytes`/`bert_file_rows` (is the file
being written), `bert_active_series`/`bert_valid_series`,
`bert_read_errors_total`/`bert_parse_errors_total`/`bert_invalid_samples_total`/
`bert_render_errors_total`. Per-link metrics carry `board`/`hybrid`/`line`
labels: `bert_bit_error_rate`, `bert_error_count`, `bert_tested_bits`,
`bert_sample_valid`, `bert_sample_timestamp_seconds`. Per-link values are `NaN`
when the DAQ logged a read-failure sentinel.

## CPX200DP commands used

`V<n> <v>`/`V<n>?`, `I<n> <i>`/`I<n>?`, `V<n>O?`/`I<n>O?` (readback),
`OP<n>`/`OPALL`, `OVP<n>`/`OCP<n>`, `LSR<n>?`, `TRIPRST`, `LOCAL`, `*IDN?`,
`*OPC?`. `/scpi` takes `{"write": "<cmd>"}` or `{"ask": "<query>"}`.
