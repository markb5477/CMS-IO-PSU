"""Voltage ramp / IV sweep on one CPX200DP channel, as a pymeasure Procedure.
shutdown() runs even on abort, so the outputs are always left off.

    python ramp.py --channel 1 --v-start 0 --v-stop 5 --v-step 0.5 \
                   --current-limit 0.5 --dwell 2 --out sweep.csv
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


class VoltageRampProcedure(Procedure):
    mode = Parameter("Mode", default="gateway")               # gateway | direct
    gateway_url = Parameter("Gateway URL", default=config.EXPORTER_URL)
    visa_resource = Parameter("VISA resource", default=config.VISA_RESOURCE)

    channel = IntegerParameter("Channel", default=1, minimum=1, maximum=2)
    current_limit = FloatParameter("Current limit", units="A", default=0.5)
    ovp = FloatParameter("Over-voltage protection", units="V", default=0.0,
                         minimum=0.0)  # 0 = leave as-is
    v_start = FloatParameter("Start voltage", units="V", default=0.0)
    v_stop = FloatParameter("Stop voltage", units="V", default=5.0)
    v_step = FloatParameter("Step voltage", units="V", default=0.5, minimum=0.001)
    dwell = FloatParameter("Dwell per point", units="s", default=2.0, minimum=0.0)

    DATA_COLUMNS = ["Voltage setpoint (V)", "Output voltage (V)",
                    "Output current (A)", "Power (W)", "Mode"]

    instrument = None
    channel_obj = None

    def startup(self):
        if self.ovp > 0 and max(self.v_start, self.v_stop) >= self.ovp:
            raise ValueError(f"OVP {self.ovp:g} V must be above the highest ramp "
                             f"voltage {max(self.v_start, self.v_stop):g} V, "
                             "or it will trip")
        self.instrument = make_instrument(self.mode, self.gateway_url, self.visa_resource)
        self.channel_obj = self.instrument.channels[self.channel]
        # Program with the output off, and set the (low) start voltage BEFORE the
        # OVP: lowering OVP under the standing setpoint latches an OVP trip.
        self.channel_obj.output_enabled = 0
        self.instrument.trip_reset()                   # clear any latched trip
        self.channel_obj.voltage_setpoint = self.v_start
        self.channel_obj.current_limit = self.current_limit
        if self.ovp > 0:
            self.channel_obj.ovp = self.ovp
        self.channel_obj.output_enabled = 1
        time.sleep(TURN_ON_SETTLE)                      # ride out the turn-on transient
        log.info("startup: ch%d, limit %.3f A, ramp %.3f -> %.3f V",
                 self.channel, self.current_limit, self.v_start, self.v_stop)

    def execute(self):
        setpoints = self._frange(self.v_start, self.v_stop, self.v_step)
        for i, v in enumerate(setpoints):
            if self.should_stop():
                log.warning("aborted by request")
                break
            self.channel_obj.voltage_setpoint = v
            self._responsive_sleep(self.dwell)

            mode = self._mode()
            if mode == "TRIP":                 # a real OVP/OCP trip latches; a
                time.sleep(0.2)                # slew/desync transient clears, so
                mode = self._mode()            # only a persistent trip is real
            self.emit("results", {
                "Voltage setpoint (V)": v,
                "Output voltage (V)": self.channel_obj.voltage,
                "Output current (A)": self.channel_obj.current,
                "Power (W)": self.channel_obj.power,
                "Mode": mode,
            })
            self.emit("progress", 100 * (i + 1) / len(setpoints))

            if mode == "TRIP":
                log.error("channel tripped at %.3f V setpoint - stopping", v)
                break

    def _mode(self):
        lsr = self.channel_obj.lsr
        return ("TRIP" if lsr & 0b01100 else
                "CC" if lsr & 0b00010 else
                "CV" if lsr & 0b00001 else "OFF")

    def shutdown(self):
        # Called even on abort/exception.
        if self.instrument is not None:
            try:
                self.instrument.all_outputs_off()
                log.info("shutdown: outputs off")
            except Exception as exc:
                log.error("shutdown could not reach the instrument: %s", exc)

    @staticmethod
    def _frange(start, stop, step):
        n = int(round(abs(stop - start) / step)) + 1
        sign = 1 if stop >= start else -1
        return [round(start + sign * step * k, 6) for k in range(n)]

    def _responsive_sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end and not self.should_stop():
            time.sleep(min(0.1, end - time.time()))


def main():
    ap = argparse.ArgumentParser(description="CPX200DP voltage ramp")
    ap.add_argument("--mode", choices=["gateway", "direct"], default="gateway")
    ap.add_argument("--gateway-url", default=config.EXPORTER_URL)
    ap.add_argument("--visa-resource", default=config.VISA_RESOURCE)
    ap.add_argument("--channel", type=int, default=1, choices=[1, 2])
    ap.add_argument("--current-limit", type=float, default=0.5)
    ap.add_argument("--ovp", type=float, default=0.0)
    ap.add_argument("--v-start", type=float, default=0.0)
    ap.add_argument("--v-stop", type=float, default=5.0)
    ap.add_argument("--v-step", type=float, default=0.5)
    ap.add_argument("--dwell", type=float, default=2.0)
    ap.add_argument("--out", default="ramp_results.csv")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    procedure = VoltageRampProcedure()
    procedure.mode = args.mode
    procedure.gateway_url = args.gateway_url
    procedure.visa_resource = args.visa_resource
    procedure.channel = args.channel
    procedure.current_limit = args.current_limit
    procedure.ovp = args.ovp
    procedure.v_start = args.v_start
    procedure.v_stop = args.v_stop
    procedure.v_step = args.v_step
    procedure.dwell = args.dwell

    results = Results(procedure, args.out)
    worker = Worker(results)
    worker.start()
    worker.join(timeout=3600)
    print(f"\nresults written to {args.out} (status: {procedure.status})")
    return 0 if procedure.status == Procedure.FINISHED else 1


if __name__ == "__main__":
    sys.exit(main())
