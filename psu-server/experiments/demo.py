"""Voltage staircase on one CPX200DP channel, as a pymeasure Procedure. Steps
through a list of levels, holding each for a few seconds, and can loop, so the
movement is easy to watch live in Grafana. shutdown() runs even on abort, so
the output is always left off.

    python demo.py --channel 1 --current-limit 0.5 --ovp 8 \
                   --levels 1,2,3,4,5,4,3,2,1 --dwell 4 --cycles 2 --out demo.csv
"""

import argparse
import logging
import os
import sys
import time

from pymeasure.experiment import (FloatParameter, IntegerParameter, Parameter,
                                   Procedure, Results, Worker)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from cpx200dp import CPX200DP

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

# The CPX briefly asserts the OVP bit during the turn-on transient; wait for the
# rail to settle before reading the Limit Status Register, or a clean start
# reads as a trip.
TURN_ON_SETTLE = 0.5


def make_instrument(mode, gateway_url, visa_resource):
    if mode == "gateway":
        from gateway import GatewayAdapter
        return CPX200DP(GatewayAdapter(gateway_url))
    from pymeasure.adapters import VISAAdapter
    return CPX200DP(VISAAdapter(visa_resource,
                                read_termination="\n", write_termination="\n"))


class StaircaseProcedure(Procedure):
    mode = Parameter("Mode", default="gateway")               # gateway | direct
    gateway_url = Parameter("Gateway URL", default=config.EXPORTER_URL)
    visa_resource = Parameter("VISA resource", default="TCPIP::192.168.0.100::9221::SOCKET")

    channel = IntegerParameter("Channel", default=1, minimum=1, maximum=2)
    current_limit = FloatParameter("Current limit", units="A", default=0.5)
    ovp = FloatParameter("Over-voltage protection", units="V", default=0.0,
                         minimum=0.0)  # 0 = leave as-is
    levels = Parameter("Levels (V)", default="1,2,3,4,5,4,3,2,1")
    dwell = FloatParameter("Dwell per level", units="s", default=4.0, minimum=0.1)
    sample = FloatParameter("Sample interval", units="s", default=1.0, minimum=0.1)
    cycles = IntegerParameter("Cycles", default=1, minimum=1)

    DATA_COLUMNS = ["Time (s)", "Voltage setpoint (V)", "Output voltage (V)",
                    "Output current (A)", "Power (W)", "Mode"]

    instrument = None
    channel_obj = None

    def startup(self):
        self._levels = [float(x) for x in str(self.levels).split(",") if x.strip()]
        if not self._levels:
            raise ValueError("no levels given")
        if self.ovp > 0 and max(self._levels) >= self.ovp:
            raise ValueError(f"OVP {self.ovp:g} V must be above the highest "
                             f"level {max(self._levels):g} V, or it will trip")
        self.instrument = make_instrument(self.mode, self.gateway_url, self.visa_resource)
        self.channel_obj = self.instrument.channels[self.channel]
        # Program with the output off, and set the (low) start voltage BEFORE the
        # OVP: lowering OVP under the standing setpoint latches an OVP trip.
        self.channel_obj.output_enabled = 0
        self.instrument.trip_reset()                   # clear any latched trip, output off
        self.channel_obj.voltage_setpoint = self._levels[0]
        self.channel_obj.current_limit = self.current_limit
        if self.ovp > 0:
            self.channel_obj.ovp = self.ovp
        self.channel_obj.output_enabled = 1
        time.sleep(TURN_ON_SETTLE)                      # ride out the turn-on transient
        log.info("startup: ch%d, limit %.3f A, %d level(s) x %d cycle(s)",
                 self.channel, self.current_limit, len(self._levels), self.cycles)

    def execute(self):
        t0 = time.time()
        steps = [(c, v) for c in range(self.cycles) for v in self._levels]
        for i, (_, v) in enumerate(steps):
            if self.should_stop():
                log.warning("aborted by request")
                break
            self.channel_obj.voltage_setpoint = v
            if self._hold(v, t0) == "TRIP":
                log.error("channel tripped at %.3f V setpoint - stopping", v)
                break
            self.emit("progress", 100 * (i + 1) / len(steps))

    def _mode(self):
        lsr = self.channel_obj.lsr
        return ("TRIP" if lsr & 0b01100 else
                "CC" if lsr & 0b00010 else
                "CV" if lsr & 0b00001 else "OFF")

    def _hold(self, v, t0):
        """Hold setpoint v for one dwell, emitting a row every `sample` seconds.
        Returns the last mode seen ('CV'/'CC'/'TRIP'/'OFF' or 'STOP')."""
        end = time.time() + self.dwell
        while True:
            mode = self._mode()
            if mode == "TRIP":                 # a real OVP/OCP trip latches; a
                time.sleep(0.2)                # slew/desync transient clears, so
                mode = self._mode()            # only a persistent trip is real
            v_out, i_out = self.channel_obj.voltage, self.channel_obj.current
            self.emit("results", {
                "Time (s)": round(time.time() - t0, 1),
                "Voltage setpoint (V)": v,
                "Output voltage (V)": v_out,
                "Output current (A)": i_out,
                "Power (W)": v_out * i_out,
                "Mode": mode,
            })
            if mode == "TRIP":
                return "TRIP"
            if time.time() >= end or self.should_stop():
                return "STOP" if self.should_stop() else mode
            time.sleep(min(self.sample, max(0.0, end - time.time())))

    def shutdown(self):
        # Called even on abort/exception.
        if self.instrument is not None:
            try:
                self.instrument.all_outputs_off()
                log.info("shutdown: outputs off")
            except Exception as exc:
                log.error("shutdown could not reach the instrument: %s", exc)


def main():
    ap = argparse.ArgumentParser(description="CPX200DP voltage staircase")
    ap.add_argument("--mode", choices=["gateway", "direct"], default="gateway")
    ap.add_argument("--gateway-url", default=config.EXPORTER_URL)
    ap.add_argument("--visa-resource", default="TCPIP::192.168.0.100::9221::SOCKET")
    ap.add_argument("--channel", type=int, default=1, choices=[1, 2])
    ap.add_argument("--current-limit", type=float, default=0.5)
    ap.add_argument("--ovp", type=float, default=0.0)
    ap.add_argument("--levels", default="1,2,3,4,5,4,3,2,1",
                    help="comma-separated voltage setpoints, in order")
    ap.add_argument("--dwell", type=float, default=4.0)
    ap.add_argument("--sample", type=float, default=1.0)
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--out", default="demo_results.csv")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    procedure = StaircaseProcedure()
    procedure.mode = args.mode
    procedure.gateway_url = args.gateway_url
    procedure.visa_resource = args.visa_resource
    procedure.channel = args.channel
    procedure.current_limit = args.current_limit
    procedure.ovp = args.ovp
    procedure.levels = args.levels
    procedure.dwell = args.dwell
    procedure.sample = args.sample
    procedure.cycles = args.cycles

    results = Results(procedure, args.out)
    worker = Worker(results)
    worker.start()
    worker.join(timeout=3600)
    print(f"\nresults written to {args.out} (status: {procedure.status})")
    return 0 if procedure.status == Procedure.FINISHED else 1


if __name__ == "__main__":
    sys.exit(main())
