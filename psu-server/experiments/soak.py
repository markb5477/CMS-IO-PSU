"""Dual-channel scenario/soak on the CPX200DP, as a pymeasure Procedure. Steps
BOTH outputs through a scripted sequence of voltage / current-limit
combinations, holding each state long enough to register in Prometheus/Grafana:
ramp both up, ramp both down, asymmetric combos, one channel fixed while the
other sweeps, and different current-limit configs. 30 states x 10 s = ~5 min.
shutdown() runs even on abort, so both outputs are always left off.

    python soak.py --dwell 10 --ovp 8 --out soak.csv
"""

import argparse
import logging
import os
import sys
import time

from pymeasure.experiment import (FloatParameter, Parameter, Procedure,
                                   Results, Worker)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from cpx200dp import CPX200DP

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

# The CPX briefly asserts the OVP bit during the turn-on transient; wait for the
# rail to settle before reading the Limit Status Register, or a clean start
# reads as a trip.
TURN_ON_SETTLE = 0.5


def build_scenario():
    """List of (v1, i1, v2, i2) states. Voltages stay <= 6 V (well under the
    ceilings); currents are the limits (open terminals draw ~0 A, but the limit
    line still moves in Grafana). Held `dwell` seconds each in execute()."""
    s = []
    for v in (1, 2, 3, 4, 5, 6):                       # ramp both up together
        s.append((v, 0.5, v, 0.5))
    for v in (5, 4, 3, 2, 1):                          # ramp both down together
        s.append((v, 0.5, v, 0.5))
    for v1, v2 in ((1, 6), (2, 5), (3, 4),             # ch1 up while ch2 down
                   (4, 3), (5, 2), (6, 1)):
        s.append((v1, 0.5, v2, 0.5))
    for v2 in (1, 2, 4, 5, 6):                         # ch1 held at 3 V, ch2 sweeps
        s.append((3, 0.5, v2, 0.5))
    for i in (1.0, 2.0, 3.0, 0.5):                     # vary current-limit configs
        s.append((4, i, 2, i))
    for v in (4, 3, 2, 1):                             # ramp both down to rest
        s.append((v, 0.5, v, 0.5))
    return s


class SoakProcedure(Procedure):
    mode = Parameter("Mode", default="gateway")               # gateway | direct
    gateway_url = Parameter("Gateway URL", default=config.EXPORTER_URL)
    visa_resource = Parameter("VISA resource", default=config.VISA_RESOURCE)

    ovp = FloatParameter("Over-voltage protection", units="V", default=8.0,
                         minimum=0.0)  # 0 = leave as-is
    dwell = FloatParameter("Dwell per state", units="s", default=10.0, minimum=0.1)
    sample = FloatParameter("Sample interval", units="s", default=1.0, minimum=0.1)

    DATA_COLUMNS = ["Time (s)",
                    "CH1 setpoint (V)", "CH1 voltage (V)", "CH1 current (A)", "CH1 mode",
                    "CH2 setpoint (V)", "CH2 voltage (V)", "CH2 current (A)", "CH2 mode"]

    instrument = None
    ch1 = None
    ch2 = None

    def startup(self):
        self._scenario = build_scenario()
        vmax = max(max(v1, v2) for v1, _, v2, _ in self._scenario)
        if self.ovp > 0 and vmax >= self.ovp:
            raise ValueError(f"OVP {self.ovp:g} V must be above the highest "
                             f"scenario voltage {vmax:g} V, or it will trip")
        self.instrument = make_instrument(self.mode, self.gateway_url, self.visa_resource)
        self.ch1 = self.instrument.channels[1]
        self.ch2 = self.instrument.channels[2]
        # Program both channels with the outputs off, then enable and settle.
        self.ch1.output_enabled = 0
        self.ch2.output_enabled = 0
        self.instrument.trip_reset()                   # clear any latched trip
        v1, i1, v2, i2 = self._scenario[0]
        self.ch1.voltage_setpoint = v1
        self.ch1.current_limit = i1
        self.ch2.voltage_setpoint = v2
        self.ch2.current_limit = i2
        if self.ovp > 0:
            self.ch1.ovp = self.ovp
            self.ch2.ovp = self.ovp
        self.ch1.output_enabled = 1
        self.ch2.output_enabled = 1
        time.sleep(TURN_ON_SETTLE)                      # ride out the turn-on transient
        log.info("startup: both channels on, %d states x %.0f s (~%.0f s total)",
                 len(self._scenario), self.dwell, len(self._scenario) * self.dwell)

    def execute(self):
        t0 = time.time()
        n = len(self._scenario)
        for i, (v1, i1, v2, i2) in enumerate(self._scenario):
            if self.should_stop():
                log.warning("aborted by request")
                break
            self.ch1.voltage_setpoint = v1
            self.ch1.current_limit = i1
            self.ch2.voltage_setpoint = v2
            self.ch2.current_limit = i2
            log.info("state %d/%d: ch1 %.1f V/%.2f A, ch2 %.1f V/%.2f A",
                     i + 1, n, v1, i1, v2, i2)
            if self._hold(v1, v2, t0) == "TRIP":
                log.error("a channel tripped - stopping")
                break
            self.emit("progress", 100 * (i + 1) / n)

    def _mode(self, ch):
        """Classify a channel, debouncing trips: a real OVP/OCP trip latches,
        so a slew/desync transient clears on the re-read and isn't reported."""
        m = self._classify(ch)
        if m == "TRIP":
            time.sleep(0.2)
            m = self._classify(ch)
        return m

    @staticmethod
    def _classify(ch):
        lsr = ch.lsr
        return ("TRIP" if lsr & 0b01100 else
                "CC" if lsr & 0b00010 else
                "CV" if lsr & 0b00001 else "OFF")

    def _hold(self, v1_set, v2_set, t0):
        end = time.time() + self.dwell
        while True:
            m1, m2 = self._mode(self.ch1), self._mode(self.ch2)
            v1, i1 = self.ch1.voltage, self.ch1.current
            v2, i2 = self.ch2.voltage, self.ch2.current
            self.emit("results", {
                "Time (s)": round(time.time() - t0, 1),
                "CH1 setpoint (V)": v1_set, "CH1 voltage (V)": v1,
                "CH1 current (A)": i1, "CH1 mode": m1,
                "CH2 setpoint (V)": v2_set, "CH2 voltage (V)": v2,
                "CH2 current (A)": i2, "CH2 mode": m2,
            })
            if m1 == "TRIP" or m2 == "TRIP":
                return "TRIP"
            if time.time() >= end or self.should_stop():
                return "STOP" if self.should_stop() else "OK"
            time.sleep(min(self.sample, max(0.0, end - time.time())))

    def shutdown(self):
        # Called even on abort/exception.
        if self.instrument is not None:
            try:
                self.instrument.all_outputs_off()
                log.info("shutdown: outputs off")
            except Exception as exc:
                log.error("shutdown could not reach the instrument: %s", exc)


def make_instrument(mode, gateway_url, visa_resource):
    if mode == "gateway":
        from gateway import GatewayAdapter
        return CPX200DP(GatewayAdapter(gateway_url))
    from pymeasure.adapters import VISAAdapter
    return CPX200DP(VISAAdapter(visa_resource,
                                read_termination="\n", write_termination="\n"))


def main():
    ap = argparse.ArgumentParser(description="CPX200DP dual-channel scenario/soak")
    ap.add_argument("--mode", choices=["gateway", "direct"], default="gateway")
    ap.add_argument("--gateway-url", default=config.EXPORTER_URL)
    ap.add_argument("--visa-resource", default=config.VISA_RESOURCE)
    ap.add_argument("--ovp", type=float, default=8.0)
    ap.add_argument("--dwell", type=float, default=10.0)
    ap.add_argument("--sample", type=float, default=1.0)
    ap.add_argument("--out", default="soak_results.csv")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    procedure = SoakProcedure()
    procedure.mode = args.mode
    procedure.gateway_url = args.gateway_url
    procedure.visa_resource = args.visa_resource
    procedure.ovp = args.ovp
    procedure.dwell = args.dwell
    procedure.sample = args.sample

    results = Results(procedure, args.out)
    worker = Worker(results)
    worker.start()
    worker.join(timeout=3600)
    print(f"\nresults written to {args.out} (status: {procedure.status})")
    return 0 if procedure.status == Procedure.FINISHED else 1


if __name__ == "__main__":
    sys.exit(main())
