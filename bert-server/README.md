# bert-server — BER test status exporter

A tiny stdlib Prometheus exporter that surfaces the mm_acf continuous
bit-error-rate test to the same Grafana/Prometheus stack as the PSU monitor.
It is the direct sibling of `../psu-server/exporter.py`, but instead of polling
an instrument it **follows a CSV file** the DAQ writes.

## What it does

`OTBitErrorRateTestContinuous` (in mm_acf) appends one row per sample to
`bertContinuous.csv`:

```
timestamp,board,hybrid,line,testedBits,errorCount
2026-07-20T08:31:00Z,0,0,3,10000000000,12
```

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

## Deploy (systemd, on the LAB PC, userspace)

Mirror the PSU exporter. `../deploy-psu-server.sh` rsyncs this folder to
`~/bert-monitor` on cmsladdertest (it deploys both exporters in one run). Then
set `.env`, sanity-check the parser, and install the per-user service (no sudo):

```bash
python3 -m unittest discover -s tests    # sanity-check the parser first

mkdir -p ~/.config/systemd/user
sed -e 's|/opt/bert-monitor|%h/bert-monitor|' \
    -e 's|multi-user.target|default.target|' \
    ~/bert-monitor/systemd/bert-exporter.service > ~/.config/systemd/user/bert-exporter.service
systemctl --user enable --now bert-exporter
loginctl enable-linger $USER            # survive logout
```

## On / off (LAB PC)

Start and stop only the BER metrics polling — the mm_acf DAQ and the
`bertContinuous.csv` it writes are never touched:

```bash
cd ~/bert-monitor
./on.sh                  # start bert-exporter + show status + curl hints
./off.sh                 # stop it (also reaps a stray foreground run on :9821)
systemctl --user start|stop|restart|status bert-exporter
journalctl --user -u bert-exporter -f
```

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
