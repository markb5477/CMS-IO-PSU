# Managing the PSU

Two ways to drive the CPX200DP, both through the running `cpx-exporter` on the
lab PC (it holds the one SCPI socket). `/control` and `/scpi` are
**localhost-only**, so run these on cmsladdertest (ssh in first). Base URL is
`localhost:9820` (`HTTP_PORT` in `.env`). Setpoints are safety-checked;
over-limit commands are rejected, not clamped.

## Raw curl

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
