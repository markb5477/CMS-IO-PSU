"""pymeasure driver for the Aim-TTi CPX200DP. Works with any adapter:
GatewayAdapter (through the exporter) for deployment, or a VISA adapter
straight at TCPIP::<ip>::9221::SOCKET for bench bring-up."""

import re

from pymeasure.instruments import Channel, Instrument
from pymeasure.instruments.validators import strict_discrete_set


def _last_token(reply):
    """'V1 5.00' -> '5.00'."""
    return reply.strip().split()[-1]


def _strip_unit(reply):
    """'5.00V' -> '5.00'."""
    return re.sub(r"[A-Za-z]+$", "", reply.strip())


class CPXChannel(Channel):
    voltage_setpoint = Channel.control(
        "V{ch}?", "V{ch} %g", "Voltage setpoint [V].",
        preprocess_reply=_last_token)
    current_limit = Channel.control(
        "I{ch}?", "I{ch} %g", "Current limit [A].",
        preprocess_reply=_last_token)
    ovp = Channel.control(
        "OVP{ch}?", "OVP{ch} %g", "Over-voltage trip level [V].",
        preprocess_reply=_last_token)
    ocp = Channel.control(
        "OCP{ch}?", "OCP{ch} %g", "Over-current trip level [A].",
        preprocess_reply=_last_token)
    output_enabled = Channel.control(
        "OP{ch}?", "OP{ch} %d", "Output on (0 or 1).",
        validator=strict_discrete_set, values=[0, 1], cast=int)
    voltage = Channel.measurement(
        "V{ch}O?", "Output voltage [V].", preprocess_reply=_strip_unit)
    current = Channel.measurement(
        "I{ch}O?", "Output current [A].", preprocess_reply=_strip_unit)
    lsr = Channel.measurement("LSR{ch}?", "Limit Status Register.", cast=int)

    @property
    def constant_voltage(self):
        return bool(self.lsr & 0b00001)

    @property
    def constant_current(self):
        return bool(self.lsr & 0b00010)

    @property
    def tripped(self):
        return bool(self.lsr & 0b01100)  # OVP or OCP

    @property
    def power(self):
        return self.voltage * self.current


class CPX200DP(Instrument):
    ch_1 = Instrument.ChannelCreator(CPXChannel, 1)
    ch_2 = Instrument.ChannelCreator(CPXChannel, 2)

    def __init__(self, adapter, name="Aim-TTi CPX200DP", **kwargs):
        super().__init__(adapter, name, includeSCPI=False, **kwargs)

    def all_outputs_off(self):
        self.write("OPALL 0")

    def trip_reset(self):
        self.write("TRIPRST")
