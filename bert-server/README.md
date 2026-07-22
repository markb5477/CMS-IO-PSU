# bert-server — BER test status exporter

A tiny stdlib Prometheus exporter that surfaces the mm_acf continuous
bit-error-rate test to the same Grafana/Prometheus stack as the PSU monitor.
It is the direct sibling of `../psu-server/exporter.py`, but instead of polling
an instrument it **follows a CSV file** the DAQ writes.

## What it does

`OTBitErrorRateTestContinuous` (in mm_acf) appends one row per sample to
`bertContinuous.csv`:

```
timestamp,board,hybrid,line,testedBits,errorCount,fecUplink,fecDownlink,opticalPowerDownlink
2026-07-20T08:31:00Z,0,0,3,10000000000,12,0,0,584
```

Columns have been appended over the campaign: `fec*` at Run_43, `opticalPowerDownlink`
at Run_45. Rows with fewer columns (earlier runs) still parse and simply produce no
metric for the missing field (see "FEC counters" and "Downlink optical power" below).

Prometheus can't read a file — only HTTP. This exporter bridges the gap:

- follows the **newest** `bertContinuous.csv` (or a pinned path),
- keeps the **latest** row per `(board, hybrid, line)` link,
- reads only newly-appended bytes each poll (cheap as the file grows),
- resets automatically when a new run rotates the file,
- serves it on `GET /metrics` (port **9821**).

It keeps **no history** — Prometheus is the time-series database; it builds the
timeline by scraping this snapshot repeatedly.

## Metrics

| metric | meaning |
|---|---|
| `bert_up` | 1 if the last poll found and read a log |
| `bert_file_present` | 1 if a `bertContinuous.csv` was found |
| `bert_last_sample_timestamp_seconds` | newest row's time; `time() - this` = staleness |
| `bert_file_mtime_seconds`, `bert_file_size_bytes`, `bert_file_rows` | "is the file being written" signals |
| `bert_active_series` | distinct links in this run |
| `bert_read_errors_total`, `bert_parse_errors_total` | health counters |
| `bert_bit_error_rate{board,hybrid,line}` | errorCount / testedBits (latest) |
| `bert_error_count{...}`, `bert_tested_bits{...}` | latest raw counts |
| `bert_sample_timestamp_seconds{...}` | per-link latest sample time |
| `bert_run_tested_bits_total`, `bert_run_errors_total` | run-to-date exposure, all links |
| `bert_run_ber_upper_limit_95` | **95 % CL BER limit for the run** — the headline result |
| `bert_tested_bits_total{...}`, `bert_errors_total{...}`, `bert_samples_total{...}` | run-to-date, per link |
| `bert_ber_upper_limit_95{...}` | 95 % CL BER limit per link |
| `bert_fec_uplink{...}`, `bert_fec_downlink{...}` | latest lpGBT FEC correction counts |
| `bert_fec_uplink_max{...}`, `bert_fec_downlink_max{...}` | worst FEC count per link this run |
| `bert_run_fec_uplink_max`, `bert_run_fec_downlink_max` | worst FEC count on any link this run |
| `bert_optical_power_downlink_adc{...}` | latest downlink optical power (raw ADC counts) |
| `bert_optical_power_downlink_adc_min{...}` | lowest optical power per link this run |
| `bert_run_optical_power_downlink_adc_min` | lowest optical power on any link this run |

### FEC counters

FEC repairs bit flips *silently*, so a climbing FEC count is link degradation the
PRBS error count cannot see — `errorCount` stays 0 precisely because FEC fixed the
frame. That makes it the useful early-warning signal next to the BER limit.

Two things shape how it is exported:

- **Never summed, only maxed.** The counter is per *optical group*, so the DAQ
  writes the same value on every hybrid row sharing that group (hybrids 22 and 23
  in the magnet-test config). A sum would multiply-count it, so the run figure is
  the worst link.
- **Absent ≠ zero.** A pre-Run_43 CSV has no FEC column, and claiming "0 FEC
  errors" from a file that never measured them would be a lie. Missing columns
  produce no per-link series and a NaN run maximum, which renders as an empty
  panel.

A malformed FEC field degrades to "not reported" rather than dropping the row —
the BER measurement in columns 0-5 is the primary result and must survive a bad
FEC read. An all-ones (`0xFFFFFFFF`) FEC value is treated as the same read-failure
sentinel used for `errorCount`, but judged independently: a row whose BER is
unusable can still carry a good FEC read, and vice versa.

The exporter does not assume whether the register is free-running or reset each
read — the maximum is the right answer either way (final count if cumulative,
worst window if not).

### Downlink optical power

`opticalPowerDownlink` (Run_45 on) is the received light level at the module, read
from the lpGBT monitoring ADC. It is a **raw ADC count** (~10-bit, observed ~583),
**not** a calibrated power — there is no µW conversion in the CSV, so the metric
name carries an explicit `_adc` suffix and no panel claims a physical unit.

It mirrors the FEC handling with two deliberate differences:

- **Reduced by the minimum, not the maximum.** For received power *low* is the
  failure — a link losing light — so the run figure is the lowest reading on any
  link, and no colour threshold is set (the counts are uncalibrated, so any amber
  line would be a guess).
- **0 is a real, alarming value** (no light), so it is kept, never treated as
  "absent". Absent means the column isn't in the row at all, which yields no
  series and a NaN run minimum. An all-ones read failure still maps to NaN, judged
  independently of the BER sample.

Per optical group like FEC, so not summed — the value repeats across the hybrid
rows sharing a group.

### Why the run totals exist

Each CSV row is one ~1 s window and `testedBits` **does not accumulate** in the
file (a row is ~325 Mbit, one link at 320 Mbps). So a single row can only resolve
a BER down to `1/testedBits` ≈ 3e-9 — `bert_bit_error_rate` reading 0 just means
"no error in that second".

The useful figure is cumulative, and it cannot be recovered in PromQL: the gauge
is scraped every few seconds while each link reports every ~10 s, so summing it
would multiply-count and miss windows. The exporter therefore sums every valid
row as it reads it. Invalid/sentinel rows are excluded, and the totals reset when
the DAQ starts a new `Run_<N>` — so the limit always describes the current run.
Totals survive a restart because the exporter re-reads the file from the start.

With zero errors observed the limit is `2.9957 / bits tested` (one-sided Poisson,
exact for k=0); with errors it uses a Wilson-Hilferty approximation of
`0.5·χ²₀.₉₅(2k+2)`, within 1 % of exact and stdlib-only.

## Pointing it at the CSV (on-site)

The DAQ writes to a run-specific directory:

```
<cwd or $GIPHT_RESULT_FOLDER>/Results/OT_ModuleTest_<ModuleId>_Run<N>/bertContinuous.csv
```

The basename is stable, so the default (glob newest under `BERT_RESULTS_ROOT`)
tracks each new run without reconfiguration. Copy `.env.example` to `.env` and
set `BERT_RESULTS_ROOT` to wherever the DAQ is launched from. Once you know the
exact path you can instead pin `BERT_CSV=...` (it wins over the glob).

```bash
cp .env.example .env
$EDITOR .env
python3 exporter.py            # foreground, for a quick check
curl -s localhost:9821/metrics
curl -s localhost:9821/status  # JSON snapshot, incl. which file it's following
```

## Deploy (systemd, on the LAB PC)

`../deploy-psu-server.sh` rsyncs this folder to `~/bert-monitor` on
cmsladdertest (it deploys both exporters in one run). Which service scope you
install into depends on **who can read the DAQ's `Results/`**:

```bash
python3 -m unittest discover -s tests    # sanity-check the parser first
```

### Root system service (current cmsladdertest setup)

The mm_acf DAQ runs as root and writes to `/root/acf-magnet/Results`, which is
`0700` — the deploy user cannot traverse it. So the exporter runs as root. The
shipped unit already targets `/opt/bert-monitor` + `multi-user.target`, so it
installs verbatim, and no `enable-linger` is needed (it starts at boot):

```bash
cp -a /home/xtaldaq/bert-monitor/. /opt/bert-monitor/
$EDITOR /opt/bert-monitor/.env          # BERT_RESULTS_ROOT=/root/acf-magnet/Results
cp /opt/bert-monitor/systemd/bert-exporter.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bert-exporter
curl -s localhost:9821/status           # check "path" points at the newest Run_<N>
```

If a per-user copy was installed earlier, disable it first or it will hold
`:9821`: `sudo -u xtaldaq XDG_RUNTIME_DIR=/run/user/$(id -u xtaldaq) systemctl
--user disable --now bert-exporter`.

### Per-user service (no sudo)

Only if `Results/` is readable by the deploy user. Rewrite the unit for the
user scope:

```bash
mkdir -p ~/.config/systemd/user
sed -e 's|/opt/bert-monitor|%h/bert-monitor|' \
    -e 's|multi-user.target|default.target|' \
    ~/bert-monitor/systemd/bert-exporter.service > ~/.config/systemd/user/bert-exporter.service
systemctl --user enable --now bert-exporter
loginctl enable-linger $USER            # survive logout
```

Note the exporter only ever **reads** the CSV, in either scope.

## On / off (LAB PC)

Start and stop only the BER metrics polling — the mm_acf DAQ and the
`bertContinuous.csv` it writes are never touched:

```bash
cd /opt/bert-monitor      # or ~/bert-monitor for a per-user install
./on.sh                   # start bert-exporter + show status + curl hints
./off.sh                  # stop it (also reaps a stray foreground run on :9821)
systemctl start|stop|restart|status bert-exporter
journalctl -u bert-exporter -f
```

`on.sh`/`off.sh` detect which scope the unit is installed in and act on that
one; drop the `--user` from the raw `systemctl`/`journalctl` calls above only
for the root system install (keep it for the per-user one).

`off.sh` reaps strays by **port** (`:9821`), so it never touches the PSU
exporter even though both files are called `exporter.py`.

On the **monitor PC** the BER data is pulled automatically: `monitoring/on.sh`
brings up the psu-tunnel forward (`:9821`) and the `bert_status` Prometheus job;
`monitoring/off.sh` tears both scrape paths down, and `--wipe` archives the BER
series together with the PSU series in the one Prometheus TSDB tarball.

## Tests

```bash
cd bert-server
python3 -m unittest discover -s tests -v   # parser / rotation / rendering
python3 tests/fake_bert_csv.py /tmp/bert/Results/run1/bertContinuous.csv --live  # fake feed
```
