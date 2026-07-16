# Guide: running an experiment on the PSU

Everything here runs against the real instrument through the exporter, so it
goes through the same safety checks as monitoring.

## 0. Moving the rig

Moving the PC and PSU to another room shouldn't break the setup, as long as you
know how the two network links hang together.

The PC reaches CERN over its built-in NIC (`enp0s31f6`). Prometheus finds it by
the hostname `cmsladdertest.dyndns.cern.ch` rather than an IP, so whatever
address it picks up in the new room sorts itself out once dyndns catches up.
Nothing to do there.

The PSU isn't on the CERN network at all. It sits on a USB-ethernet dongle on a
private `192.168.50.0/24` link that only the PC uses:

```
PSU        192.168.50.2      static, set on the LXI web page (or front panel)
PC dongle  192.168.50.1/24   plus 169.254.0.1/16, see below
```

The PSU is pinned static-only, with DHCP and AUTO-IP switched off in its LAN
config, so it can't wander onto another address. The PC end is a NetworkManager
profile `psu-link` tied to the dongle's MAC (`00:23:57:5c:28:98`), so it follows
the dongle into whichever USB port you use. `PSU_HOST=192.168.50.2` in `.env`.

Leave the `169.254.0.1/16` address on the dongle. If the PSU ever gets
factory-reset it falls back to a `169.254.x` link-local address, and that second
address is what lets the PC still reach it to put it right (`avahi-resolve -n
t527059.local.` or `ip neigh show dev <dongle>` to find it, then re-pin over
`ssh -L 8081:<ip>:80 ...`).

Once you're up in the new room, check it from the PC:

```sh
ip -br addr | grep -E '192.168.50|169.254'    # dongle has both addresses
ping -c2 192.168.50.2
systemctl --user restart cpx-exporter
curl -s localhost:9820/metrics | grep '^cpx_up'   # cpx_up 1 means it's polling
```

Don't test with `nc` on 9221 while the exporter is up: the CPX only allows one
SCPI socket and the exporter has it, so `nc` just times out. Watch `cpx_up`
instead. And don't forget the ethernet cable between the dongle and the PSU.

## 1. A manual experiment (change V by hand and confirm it took)

Two things can go wrong: the instrument rejects the command, or it accepts it
but isn't doing what you meant. Check both - the setpoint query proves it was
accepted, the output readback proves it's real.

Program with the output off, then switch on:

```sh
psuctl status                  # note the starting state
psuctl off 1
psuctl set 1 -v 5.0 -i 0.5     # prints the resulting state
psuctl status                  # V_SET should read 5.00, I_SET 0.500
psuctl on 1
psuctl status                  # output on: V_OUT tracks V_SET in CV
```

`psuctl set` re-polls and prints the state right after the write, so that echo
is your first check. `V_SET`/`I_SET` are what the instrument was *told*;
`V_OUT`/`I_OUT` are what the meter *measures*. A change is confirmed when the
setpoint matches what you sent **and**, with the output on, the measurement
tracks it (V_OUT ~ V_SET in CV, I_OUT ~ I_SET in CC).

Front-panel changes show up too - the exporter polls at 1 Hz, so a knob turn
appears in `psuctl status` (or Grafana) within a second.

To interrogate the instrument directly (localhost only):

```sh
curl -s localhost:9820/scpi -d '{"ask":"V1?"}'    # -> {"reply": "V1 5.00"}
curl -s localhost:9820/scpi -d '{"ask":"EER?"}'   # -> "0" means no error
```

`EER?` is the instrument's execution-error register: non-zero means it
rejected the last command (out of range, bad syntax). The exporter blocks most
bad values before they get this far, so `EER?` mainly matters for anything
typed on the front panel or sent raw.

## 2. An automated experiment (a scripted V ramp)

For anything more than a couple of points, script it. `experiments/ramp.py` is
a pymeasure `Procedure` that steps the voltage from `--v-start` to `--v-stop`,
dwells at each point, logs V/I/power and the CV/CC/TRIP mode to a CSV, and
(crucially) runs `shutdown()` even on abort or a trip, so the output is always
left off. It drives the PSU through the exporter, so the same safety ceilings
and envelope apply.

One-off setup (venv, once per machine):

```sh
python3 -m venv ~/cpx-venv
~/cpx-venv/bin/pip install -r experiments/requirements.txt
```

Run a 0-5 V ramp on channel 1, 0.5 A limit, 0.5 V steps, 2 s dwell, OVP at 6 V:

```sh
cd experiments
~/cpx-venv/bin/python ramp.py --channel 1 --current-limit 0.5 \
    --v-start 0 --v-stop 5 --v-step 0.5 --dwell 2 --ovp 6 --out sweep.csv
```

Each row lands in `sweep.csv` as it's taken, and the run also shows live in
Grafana (it's the same instrument the exporter is polling). If a point trips
OVP/OCP the run records a `TRIP` row and stops. Confirm the outputs are off at
the end with `psuctl status`.

For bench bring-up without the exporter running, `--mode direct
--visa-resource TCPIP::<ip>::9221::SOCKET` talks straight to the PSU (needs
`~/cpx-venv/bin/pip install pyvisa-py`). This bypasses the safety layer, so use
it only when you know the setpoints are safe.

## 3. Write a new experiment

An experiment is a pymeasure `Procedure` subclass: declare its `Parameter`s and
the `DATA_COLUMNS` it logs, then fill in three methods:

- `startup()` - set the limits and enable the output.
- `execute()` - take the measurements, `emit`ing a row each time, and bail out
  on `should_stop()` or a trip.
- `shutdown()` - turn the output off. pymeasure calls it however the run ends
  (finish, abort, or exception), so this is where "outputs off" belongs.

`ramp.py` is the full template. Here is a minimal one that holds a fixed
voltage and logs the current once a second until stopped or tripped:

```python
import time
from pymeasure.experiment import (FloatParameter, IntegerParameter,
                                   Procedure, Results, Worker)
from cpx200dp import CPX200DP
from gateway import GatewayAdapter


class Hold(Procedure):
    channel = IntegerParameter("Channel", default=1)
    voltage = FloatParameter("Voltage", units="V", default=2.0)
    current_limit = FloatParameter("Current limit", units="A", default=0.5)
    duration = FloatParameter("Duration", units="s", default=30)

    DATA_COLUMNS = ["Time (s)", "Current (A)"]

    def startup(self):
        self.psu = CPX200DP(GatewayAdapter())   # exporter URL from .env
        self.ch = self.psu.channels[self.channel]
        self.ch.current_limit = self.current_limit
        self.ch.voltage_setpoint = self.voltage
        self.ch.output_enabled = 1

    def execute(self):
        t0 = time.time()
        while time.time() - t0 < self.duration:
            if self.should_stop() or self.ch.tripped:
                break
            self.emit("results", {"Time (s)": round(time.time() - t0, 1),
                                  "Current (A)": self.ch.current})
            time.sleep(1)

    def shutdown(self):
        if getattr(self, "psu", None):
            self.psu.all_outputs_off()


if __name__ == "__main__":
    p = Hold()
    p.voltage = 2.0
    worker = Worker(Results(p, "hold.csv"))
    worker.start()
    worker.join()
```

Run it from `experiments/` so the imports resolve:

```sh
cd experiments
~/cpx-venv/bin/python hold.py
```

The keys you `emit` must match `DATA_COLUMNS`. Every read (`self.ch.current`,
`self.ch.tripped`, ...) and write goes through the exporter and its safety
checks, exactly like `ramp.py`.
