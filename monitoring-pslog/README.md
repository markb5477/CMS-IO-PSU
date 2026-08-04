# monitoring-pslog — Prometheus + Grafana for the pscontrol PS log

Monitoring for the power supplies on **pccmsbril06**, read from the values log
that [pscontrol](https://gitlab.cern.ch/mpari/pscontrol)'s monitor writes.

This is a **sibling** of `../monitoring`, not a replacement: its own compose
project, container names, ports and volumes, so the CPX stack and this one run
side by side on the same monitor host without touching each other.

|  | CPX stack (`../monitoring`) | this stack |
|---|---|---|
| Prometheus | :9090 | **:9091** |
| Grafana | :3000 | **:3001** |
| Exporter | cpx-exporter :9820, bert :9821 | ps_exporter **:9822** |
| Source | the instrument / a CSV | `<config>_values_<stamp>.log` |

## The data end of it

There is **no exporter to install here.** `ps_run -a monitor` on pccmsbril06
starts `ps_exporter` itself and it dies with the monitor (see `pscontrol/src/`).
This stack only scrapes it. If the monitor is not running, this stack comes up
healthy and the `ps_log` target is down — which is exactly the
`PSMonitorNotRunning` alert.

## Run it

```bash
cp .env.example .env
$EDITOR .env            # pick the scrape target, set GRAFANA_ADMIN_PASSWORD
./render.sh             # bake .env into prometheus/prometheus.yml
./on.sh                 # docker compose up -d
```

Three ways to reach the exporter, set as `PSLOG_EXPORTER_TARGET`:

| value | when |
|---|---|
| `pslog-tunnel:9822` | default — SSH sidecar to pccmsbril06 (needs `tunnel/id_ed25519`) |
| `pccmsbril06.cern.ch:9822` | direct, if `PS_EXPORTER_ADDR=0.0.0.0` there and the port is open |
| `host.docker.internal:9822` | an exporter on this same docker host (local replay, see below) |

`./on.sh` starts the tunnel sidecar only when the target actually names it, so
the other two modes don't need an SSH key at all.

Then: Grafana <http://localhost:3001> → **PS Monitoring** → *PS Monitoring —
lab_cosmic_crocs*. `./off.sh` stops the stack; the TSDB and Grafana state stay in
the named volumes and come back hot.

## The dashboard

Built around the channels that are **actually in the logs** (11: 8 LV + 3 HV),
not a generic template. Panels are split by scale rather than sharing an axis —
no dual-axis panels anywhere, so nothing is silently misread.

- **Is monitoring alive?** — running/stopped, age of the newest row, channel
  count, total power, unreadable rows.
- **CROC modules — LV** — voltage on its own panel (the 1.62 V band would be
  invisible next to the 14.5 V auxiliaries) and current.
- **CROC modules — HV bias** — bias voltage, and **leakage current**, the number
  that predicts sensor trouble. Amber guide at the 5 µA alert threshold.
- **Auxiliaries** — fans, PMTs, peltier. Current is log-scaled: these span four
  orders of magnitude (70 mA fans vs 300 µA PMT0).
- **Run envelope** — per-channel min/max/rows in a table. The exporter tracks
  these over *every* row while Prometheus samples every 10 s, so a ramp sag or
  inrush peak between two scrapes is recorded here and nowhere else.

A `Module` variable filters the CROC panels to one module. Each channel has a
fixed colour, and an HV channel inherits its module's LV colour, so `CROC3D` and
`CROC3D-HV` read as one device. Colours are the validated 8-hue categorical set
(checked for colourblind separation on the dark surface); every panel carries a
table legend with last/min/max, so no series is identified by colour alone.

### Two traps the panels warn about

- **A Keithley with its output off logs its SETPOINT voltage and exactly 0 A.**
  An idle HV channel therefore looks like a healthy −90 V line. Only the current
  distinguishes them — hence the `HVOutputOff` alert on current being exactly 0.
- **Power is signed.** HV reads negative volts × negative amps = positive watts,
  so a negative value is a real anomaly, not something to hide with `abs()`.

## Alerts

Seven rules in `prometheus/alerts.yml`. Every threshold is derived from the real
archive (16 runs, 356,113 rows, 2026-07-06…08-02) and the measured figure is
quoted in the rule:

| alert | threshold | why that number |
|---|---|---|
| `PSLogExporterDown` | scrape fails 2m | — |
| `PSMonitorNotRunning` | up but no log 5m | — |
| `PSMonitorStalled` | no new row 60s | measured cadence 6–9 s (max 9 s over 356k intervals) |
| `PSLogParseErrors` | any in 15m | exactly 0 occurred across the whole archive |
| `HVLeakageHigh` | >5 µA for 10m | worst ever observed 2.38 µA |
| `HVOutputOff` | I == 0 for 15m | CROCR19-HV really did sit like this for 854 rows |
| `CROCLowVoltage` | ±100 mV off 1.62 V | operating band is 1.609–1.631 V |

pscontrol already emails on a trip, so these deliberately cover what that cannot
see: monitoring itself dying, and slow degradation.

## Rehearsing without hardware

`replay-log.py` replays a real archived log as a live-growing one, so the whole
chain can be exercised with real values and no power supplies:

```bash
# 1. replay the archive at 30x into a fresh log
python3 replay-log.py ~/psdata/lab_cosmic_crocs/lab_cosmic_crocs_values_20260802_014650.log \
    --out /tmp/psreplay --speed 30 --loop

# 2. the exporter, bound so the Prometheus container can reach it
cd ../../pscontrol/src && PS_EXPORTER_ADDR=0.0.0.0 python3 ps_exporter.py /tmp/psreplay

# 3. this stack, pointed at the host
sed -i 's|^PSLOG_EXPORTER_TARGET=.*|PSLOG_EXPORTER_TARGET=host.docker.internal:9822|' .env
./render.sh && ./on.sh
```

Rows are re-stamped with the current time (Prometheus rejects samples far from
now); every measured column is passed through byte-for-byte.

This is how the stack was validated before deployment — see the e2e results in
the branch's commit message.
