# CMS-IO-PSU

Monitoring and control for the Aim-TTi CPX200DP power supply in 186/B-04.

```
 CPX200DP <---- cmsladdertest ----> Prometheus (OpenStack) --> Grafana
 (TCP 9221)     exporter.py :9820      scrapes /metrics
                    ^
                    | localhost
                 psuctl / experiments
```

`exporter.py` owns the single TCP connection to the PSU, polls it, and serves
`/metrics` (Prometheus), `/status`, `/control` (psuctl) and `/scpi`
(experiments). The last two are localhost-only. Every setpoint is
safety-checked first.

Everything but the experiments is Python 3 stdlib - nothing to install on the
lab PC. The tree is split by where each part runs:

- `psu-server/` - runs on cmsladdertest, next to the PSU:
  - `exporter.py` - the daemon.
  - `psuctl` - control CLI (on/off, set V/I/OVP/OCP, trip reset, local).
  - `experiments/` - pymeasure `Procedure`s (ramps, IV sweeps) in a user venv.
  - `config.py` + `.env` - shared config (host, ports, limits) that all three
    read; copy `.env.example` to `.env`.
  - `systemd/` - service unit.
- `monitoring/` - runs on the OpenStack host: `prometheus/` (scrape job +
  alerts, rendered from `.env`) and `grafana/` (dashboard).
- `deploy-psu-server.sh` - rsyncs `psu-server/` to the lab PC.
- `deploy-monitoring.sh` - renders the scrape config, then rsyncs `monitoring/`
  to the OpenStack Prometheus host (`prometheus-tk:/root/monitoring/`).

## Put the PSU on the network

CERN's network won't route to the PSU: an unregistered MAC lands in a quarantine
VLAN (`172.20.x`) the lab PC can't reach, and the USB serial port wedges on any
re-plug. So the PSU gets its own point-to-point link, its rear LAN port straight
into a USB-ethernet adapter, with the PC's built-in NIC left for Prometheus.

Both ends get a fixed address on that cable. On the PSU, set a static IP from its
LXI web page (Configure) or the front panel - `192.168.50.2`, mask
`255.255.255.0` - and untick DHCP and AUTO-IP so it actually uses the static
value. LAN changes only take effect after a power-cycle. On the PC, add a
NetworkManager profile keyed to the adapter's MAC so it survives being plugged
into a different USB port:

```sh
nmcli con add type ethernet con-name psu-link ifname '*' \
    802-3-ethernet.mac-address 00:23:57:5c:28:98 \
    ipv4.method manual ipv4.addresses "192.168.50.1/24,169.254.0.1/16" \
    ipv4.never-default yes ipv6.method ignore
```

The spare `169.254.0.1/16` is a fallback: if the PSU is ever factory-reset it
drops back to a link-local address (advertised over mDNS as `t527059.local`),
and that second address is how you get back to it. Check comms with `echo
"*IDN?" | nc 192.168.50.2 9221` should give back `THURLBY THANDAR, CPX200DP,
...`. Stop the
exporter first, it holds the one SCPI socket while running. GUIDE.md has the full
moving-the-rig checklist.

## On the lab PC

`./deploy-psu-server.sh` rsyncs `psu-server/` to `~/cpx-psu-monitor/`. Set the config
once (the exporter, psuctl and experiments all read this `.env`):

```sh
cd ~/cpx-psu-monitor
cp .env.example .env
$EDITOR .env          # PSU_HOST, ports, MAX_VOLTAGE/MAX_CURRENT
```

Read-only check first - confirms comms without touching any setpoint or output,
so it is safe on the live PSU:

```sh
python3 tests/readonly.py --direct     # straight to the PSU (exporter stopped)
python3 tests/readonly.py              # or via a running exporter (read-only HTTP)
python3 tests/write_check.py           # control check: writes tiny setpoints to
                                       # a channel, verifies, then restores them
```

Foreground test (flags still override the `.env` for one-offs):

```sh
python3 exporter.py --max-voltage 30 --max-current 5
curl localhost:9820/metrics
./psuctl status
```

Install as a service, with sudo:

```sh
sudo cp -r ~/cpx-psu-monitor /opt/cpx-psu-monitor
sudo cp /opt/cpx-psu-monitor/systemd/cpx-exporter.service /etc/systemd/system/
sudo systemctl enable --now cpx-exporter
```

The service reads `/opt/cpx-psu-monitor/.env`; edit it and
`sudo systemctl restart cpx-exporter` to change the config.

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

Turn the monitoring on/off (run on the lab PC - the exporter is the user
service above; the PSU outputs are never touched by start/stop):

```sh
cd ~/cpx-psu-monitor
./on.sh                 # systemctl --user start cpx-exporter  + shows status
./off.sh                # systemctl --user stop  cpx-exporter
# under the hood, equivalently:
systemctl --user start|stop|restart|status cpx-exporter
journalctl --user -u cpx-exporter -f      # follow logs
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

- A remote command locks the front panel; `psuctl local` hands it back and
  pauses polling for `--pause` seconds (else the next poll re-locks it).
- `/control` is localhost-only, so control = ssh access. Metrics are open.
- Settings persist in the instrument; the exporter going down leaves the
  outputs untouched.
- Every setpoint is checked before it reaches the PSU: the CPX200DP hardware
  ranges (0-60 V, 0-10 A, 180 W PowerFlex envelope, OVP/OCP ranges), then the
  `--max-voltage`/`--max-current` ceilings. Over-limit or malformed commands
  are rejected, not clamped; ceilings can only tighten the hardware limits.

## Experiments

pymeasure `Procedure`s run in a venv on cmsladdertest and drive the PSU
through the exporter. `shutdown()` runs even on abort, so outputs are always
left off.

```sh
python3 -m venv ~/cpx-venv
~/cpx-venv/bin/pip install -r ~/cpx-psu-monitor/experiments/requirements.txt
cd ~/cpx-psu-monitor/experiments
~/cpx-venv/bin/python ramp.py --channel 1 --current-limit 0.5 \
    --v-start 0 --v-stop 5 --v-step 0.5 --dwell 2 --ovp 6 --out sweep.csv
```

For bench bring-up, `--mode direct --visa-resource TCPIP::<ip>::9221::SOCKET`
talks straight to the PSU (needs `pip install pyvisa-py`). Write your own by
subclassing `Procedure` - see `ramp.py`. `GUIDE.md` walks through a manual and
an automated experiment step by step.

## Prometheus + Grafana (OpenStack)

Everything for the monitoring host is under `monitoring/`, run as a portable
Docker stack (Prometheus + Grafana). `deploy-monitoring.sh` renders the config
from `.env` and rsyncs the folder to `prometheus-tk:/root/monitoring/`; the
scrape job, alert rules, Prometheus datasource and dashboard are all wired in
automatically - nothing to merge or import by hand.

From your laptop, set the scrape target once and deploy:

```sh
cd monitoring
cp .env.example .env
$EDITOR .env            # PSU_EXPORTER_TARGET, interval/labels, ports, GRAFANA_ADMIN_PASSWORD
cd .. && ./deploy-monitoring.sh
```

Then on the box (`ssh prometheus-tk`), turn the stack on/off - it runs there
directly (needs Docker + the compose plugin):

```sh
cd /root/monitoring
cp .env.example .env    # first time only: set GRAFANA_ADMIN_PASSWORD / ports
./on.sh                 # docker compose up -d  (Prometheus :9090, Grafana :3000)
./off.sh                # docker compose down   (--wipe to also drop stored data)
```

`render.sh` (run for you by the deploy) writes `prometheus/prometheus.yml` from
`prometheus/prometheus.yml.tmpl`; edit the template, not the output.

For a browserless view, `dash.py` renders a live terminal dashboard from the
Prometheus API (stdlib only) - run it on the box:

```sh
python3 /root/monitoring/dash.py --watch      # live, Ctrl-C to quit
python3 /root/monitoring/dash.py              # one-shot snapshot
```

### Scrape path (reverse tunnel)

A direct scrape of `cmsladdertest:9820` is blocked by the CERN firewall, so the
stack includes a `psu-tunnel` sidecar (`tunnel/`, autossh): it SSHes *out* to
`xtaldaq@cmsladdertest` and local-forwards the exporter, and Prometheus scrapes
`psu-tunnel:9820` over the internal Docker network (`PSU_EXPORTER_TARGET` in
`.env`). One-time key setup on the box:

```sh
cd /root/monitoring
ssh-keygen -t ed25519 -f tunnel/id_ed25519 -N "" -C "psu-tunnel@prometheus"
cat tunnel/id_ed25519.pub      # append this to ~xtaldaq/.ssh/authorized_keys on the lab PC
```

The key lives only on the box (gitignored, and excluded from the deploy rsync so
redeploys don't clobber it). If a firewall opening for 9820 is ever granted, set
`PSU_EXPORTER_TARGET=cmsladdertest.dyndns.cern.ch:9820` and drop the sidecar.

## Metrics

`cpx_up`, `cpx_info{idn}`, `cpx_set_voltage_volts`/`cpx_set_current_amps`,
`cpx_output_voltage_volts`/`cpx_output_current_amps`, `cpx_output_power_watts`,
`cpx_output_enabled`, `cpx_constant_voltage`/`cpx_constant_current`,
`cpx_trip_overvoltage`/`cpx_trip_overcurrent`, `cpx_power_limit`,
`cpx_limit_status_register`, `cpx_max_voltage_volts`/`cpx_max_current_amps`,
`cpx_poll_errors_total`, `cpx_poll_duration_seconds`, `cpx_polling_paused`.
Per-channel metrics carry a `channel` label. Poll interval 1 s (meters
update at 4 Hz).

## CPX200DP commands used

`V<n> <v>`/`V<n>?`, `I<n> <i>`/`I<n>?`, `V<n>O?`/`I<n>O?` (readback),
`OP<n>`/`OPALL`, `OVP<n>`/`OCP<n>`, `LSR<n>?`, `TRIPRST`, `LOCAL`, `*IDN?`,
`*OPC?`. `/scpi` takes `{"write": "<cmd>"}` or `{"ask": "<query>"}`.
