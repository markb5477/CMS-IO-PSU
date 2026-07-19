"""In-memory stand-in for the CPX200DP, matching the CPX driver interface the
Monitor uses (connected / connect / close / send / query). It lets the guard
and command-construction tests run the *real* exporter code without a socket or
live hardware, so the suite is safe to run anywhere (never touches PSU_HOST).

It is deliberately simple physics: a channel's terminal voltage tracks its
setpoint when the output is on and reads ~0 when off; output current stays 0
(no load). That is enough to exercise every command path and readback the
control/poll code depends on - it is not a faithful instrument model.
"""

import re

IDN = "THURLBY THANDAR, CPX200DP, 0, 1.00 (fake)"


class FakeCPX:
    def __init__(self):
        # per-channel state; keyed by channel int
        self.ch = {n: {"v": 0.0, "i": 1.0, "op": 0, "lsr": 0, "ovp": 60.0, "ocp": 10.0}
                   for n in (1, 2)}
        self._connected = False
        self.sent = []          # every raw command that reached the wire
        self.fail_on_send = None  # set to a cmd substring to simulate an I/O error

    # --- driver interface -------------------------------------------------
    @property
    def connected(self):
        return self._connected

    def connect(self):
        self._connected = True

    def close(self):
        self._connected = False

    def send(self, cmd):
        if not self._connected:
            raise ConnectionError("not connected to instrument")
        if self.fail_on_send and self.fail_on_send in cmd:
            raise OSError(f"simulated I/O failure on {cmd!r}")
        self.sent.append(cmd)
        self._apply(cmd)

    def query(self, cmd):
        if not self._connected:
            raise ConnectionError("not connected to instrument")
        self.sent.append(cmd)
        return self._answer(cmd)

    # --- fake instrument behaviour ---------------------------------------
    def _apply(self, cmd):
        m = re.fullmatch(r"\s*V([12])\s+([-\d.]+)\s*", cmd, re.I)
        if m:
            self.ch[int(m.group(1))]["v"] = float(m.group(2))
            return
        m = re.fullmatch(r"\s*I([12])\s+([-\d.]+)\s*", cmd, re.I)
        if m:
            self.ch[int(m.group(1))]["i"] = float(m.group(2))
            return
        m = re.fullmatch(r"\s*OP([12])\s+([01])\s*", cmd, re.I)
        if m:
            self.ch[int(m.group(1))]["op"] = int(m.group(2))
            return
        m = re.fullmatch(r"\s*OPALL\s+([01])\s*", cmd, re.I)
        if m:
            for c in self.ch.values():
                c["op"] = int(m.group(1))
            return
        m = re.fullmatch(r"\s*OVP([12])\s+([-\d.]+)\s*", cmd, re.I)
        if m:
            self.ch[int(m.group(1))]["ovp"] = float(m.group(2))
            return
        m = re.fullmatch(r"\s*OCP([12])\s+([-\d.]+)\s*", cmd, re.I)
        if m:
            self.ch[int(m.group(1))]["ocp"] = float(m.group(2))
            return
        if re.fullmatch(r"\s*TRIPRST\s*", cmd, re.I):
            for c in self.ch.values():
                c["lsr"] &= ~0b1100  # clear the two trip bits
            return
        # LOCAL / *OPC? writes need no state change for these tests

    def _answer(self, cmd):
        cmd = cmd.strip()
        if cmd == "*IDN?":
            return IDN
        if cmd == "*OPC?":
            return "1"
        m = re.fullmatch(r"V([12])\?", cmd, re.I)
        if m:
            return f"V{m.group(1)} {self.ch[int(m.group(1))]['v']:.2f}"
        m = re.fullmatch(r"I([12])\?", cmd, re.I)
        if m:
            return f"I{m.group(1)} {self.ch[int(m.group(1))]['i']:.3f}"
        m = re.fullmatch(r"V([12])O\?", cmd, re.I)
        if m:
            c = self.ch[int(m.group(1))]
            return f"{(c['v'] if c['op'] else 0.0):.2f}V"
        m = re.fullmatch(r"I([12])O\?", cmd, re.I)
        if m:
            return "0.000A"
        m = re.fullmatch(r"OVP([12])\?", cmd, re.I)
        if m:
            return f"VP{m.group(1)} {self.ch[int(m.group(1))]['ovp']:.1f}"
        m = re.fullmatch(r"OCP([12])\?", cmd, re.I)
        if m:
            return f"CP{m.group(1)} {self.ch[int(m.group(1))]['ocp']:.2f}"
        m = re.fullmatch(r"OP([12])\?", cmd, re.I)
        if m:
            return str(self.ch[int(m.group(1))]["op"])
        m = re.fullmatch(r"LSR([12])\?", cmd, re.I)
        if m:
            c = self.ch[int(m.group(1))]
            # trip bits (bits 2/3) plus a constant-voltage bit while the output is
            # on (open terminals draw ~0 A, so never current-limited)
            return str(c["lsr"] | (0b1 if c["op"] else 0))
        raise ValueError(f"fake instrument got unexpected query {cmd!r}")
