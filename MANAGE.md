# Managing the PSU

Two ways to drive the CPX200DP, both through the running `cpx-exporter` on the
lab PC (it holds the one SCPI socket). `/control` and `/scpi` are
**localhost-only**, so run these on cmsladdertest (ssh in first). Base URL is
`localhost:9820` (`HTTP_PORT` in `.env`). Setpoint guards are **currently
disabled** (limits raised to 100000 in `exporter.py` and `.env`), so nothing is
rejected in software — only the instrument's own limits apply. See the safety
note in README.md to re-enable them.

## Raw curl
Documentation: http://resources.aimtti.com/manuals/CPX200D+DP_Instruction_Manual-Iss8.pdf
```sh
# Read-only. /status and /metrics are open, not localhost-only
curl -s localhost:9820/status                  # live state, JSON
curl -s localhost:9820/metrics                 # Prometheus text

# Control. POST JSON to /control with an "action"
curl -s -X POST localhost:9820/control -d '{"action":"on","channel":"1"}'   # 1 | 2 | all
curl -s -X POST localhost:9820/control -d '{"action":"off","channel":"all"}'

# set: any of voltage/current/ovp/ocp, channel 1 or 2
curl -s -X POST localhost:9820/control \
  -d '{"action":"set","channel":"1","voltage":5.0,"current":0.5,"ovp":6.0,"ocp":1.0}'

curl -s -X POST localhost:9820/control -d '{"action":"triprst"}'            # clear a trip
curl -s -X POST localhost:9820/control -d '{"action":"local","pause":120}'  # hand back front panel
```

Control replies with JSON `{"sent": [...], "state": {...}}`. HTTP 400 is
rejected (bad/over-limit), 403 is not localhost, 503 is PSU unreachable.

Raw SCPI (experiments; also localhost-only):

```sh
curl -s localhost:9820/scpi -d '{"ask":"*IDN?"}'    # query  -> {"reply": "..."}
curl -s localhost:9820/scpi -d '{"write":"V1 5.0"}' # write  -> {"ok": true, ...}
```

## psuctl

Same `/control` calls, wrapped. Reads the exporter URL from `.env`. Run from
`~/cpx-psu-monitor`:

```sh
./psuctl status                   # state table
./psuctl on 1                     # on/off  1 | 2 | all
./psuctl off all
./psuctl set 1 -v 5.0 -i 0.5      # 5 V, 0.5 A limit on channel 1
./psuctl set 1 --ovp 6.0 --ocp 1.0
./psuctl reset                    # clear an OVP/OCP trip
./psuctl local --pause 120        # unlock front panel for 120s (default 60)
```

Any remote command re-locks the front panel. `psuctl local` hands it back and
pauses polling so it stays local. Use `--url http://host:port` for a non-default
exporter. The exporter going down leaves PSU outputs untouched.

## Managing the BER exporter

The BER exporter (:9821, `BERT_HTTP_PORT`) is **read-only** — it has no
`/control` or `/scpi`, and never writes to the DAQ's CSV. There is nothing to
drive; managing it means checking what it is following and repointing it.

```sh
curl -s localhost:9821/status            # JSON: which file, rows, per-link samples
curl -s localhost:9821/metrics           # Prometheus text
curl -s localhost:9821/status | grep -m1 path     # <- the CSV it is following
```

Repoint it at a different CSV (config lives in `~/bert-monitor/.env`, see
README.md "Pointing the BER exporter at the CSV"):

```sh
cd ~/bert-monitor
$EDITOR .env             # BERT_RESULTS_ROOT (glob, default) or BERT_CSV (pin)
systemctl --user restart bert-exporter
curl -s localhost:9821/status | grep -m1 path     # confirm it took
```

Start/stop, without touching the PSU exporter (both processes are called
`exporter.py`; the scripts key on the port, not the name):

```sh
./on.sh | ./off.sh
systemctl --user start|stop|restart|status bert-exporter
journalctl --user -u bert-exporter -f
```

A one-off run against some other file, without editing `.env`:

```sh
BERT_CSV=/path/to/bertContinuous.csv python3 exporter.py --port 9821
```
