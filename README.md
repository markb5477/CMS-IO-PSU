# CMS-IO-PSU

Monitoring and control for the Aim-TTi CPX200DP power supply in 186/B-04.

```
 CPX200DP <---- cmsladdertest ----> Prometheus (OpenStack) --> Grafana
 (TCP 9221)     exporter.py :9820      scrapes /metrics
                    ^
                    | localhost
                 psuctl / experiments
```

`exporter.py` holds the one TCP connection to the PSU, polls it, and serves
`/metrics`, `/status`, `/control` (psuctl) and `/scpi` (experiments). The last
two are localhost-only. Setpoints are safety-checked before they reach the PSU.

Layout by where it runs:

- `psu-server/` runs on cmsladdertest, next to the PSU:
  - `exporter.py`, the daemon
  - `psuctl`, control CLI (on/off, set V/I/OVP/OCP, trip reset, local)
  - `experiments/`, pymeasure `Procedure`s (ramps, IV sweeps), in a venv
  - `config.py` + `.env`, shared config (host, ports, limits); copy `.env.example`
  - `systemd/`, service unit
- `monitoring/` runs on the OpenStack host: `prometheus/` (scrape + alerts) and `grafana/`
- `deploy-psu-server.sh` rsyncs `psu-server/` to the lab PC
- `deploy-monitoring.sh` renders the scrape config, rsyncs `monitoring/` to `prometheus-tk:/root/monitoring/`

Python 3 stdlib only, except the experiments. Nothing to install on the lab PC.

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

Open TCP 9820 to the Prometheus host if a firewall is running.

Monitoring on/off (the exporter is the user service above; PSU outputs are never
touched by start/stop):

```sh
cd ~/cpx-psu-monitor
./on.sh                 # start cpx-exporter + show status
./off.sh                # stop cpx-exporter
systemctl --user start|stop|restart|status cpx-exporter
journalctl --user -u cpx-exporter -f
```

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
- Every setpoint is checked first: CPX200DP hardware ranges (0-60 V, 0-10 A,
  180 W PowerFlex envelope, OVP/OCP ranges), then the `--max-voltage`/
  `--max-current` ceilings. Over-limit commands are rejected, not clamped;
  ceilings only tighten the hardware limits.

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

For bench bring-up, `--mode direct --visa-resource TCPIP::<ip>::9221::SOCKET`
talks straight to the PSU (needs `pip install pyvisa-py`). Subclass `Procedure`
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
./off.sh                # down   (--wipe to also drop stored data)
```

`render.sh` (run by the deploy) writes `prometheus/prometheus.yml` from
`prometheus.yml.tmpl`. Edit the template, not the output.

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

## CPX200DP commands used

`V<n> <v>`/`V<n>?`, `I<n> <i>`/`I<n>?`, `V<n>O?`/`I<n>O?` (readback),
`OP<n>`/`OPALL`, `OVP<n>`/`OCP<n>`, `LSR<n>?`, `TRIPRST`, `LOCAL`, `*IDN?`,
`*OPC?`. `/scpi` takes `{"write": "<cmd>"}` or `{"ask": "<query>"}`.
