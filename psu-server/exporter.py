#!/usr/bin/env python3
"""Prometheus exporter + control daemon for the Aim-TTi CPX200DP.

Holds the one TCP connection to the PSU (port 9221), polls it, and serves:

  GET  /metrics   Prometheus format
  GET  /status    last poll as JSON
  POST /control   control, localhost-only by default
  POST /scpi      raw SCPI, localhost-only (experiments)

Setpoints are range- and envelope-checked before reaching the PSU, and
rejected rather than clamped. Stdlib only.
"""

import argparse
import json
import re
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config


class HTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # skip the reverse-DNS lookup that stalls startup by ~5s
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

CHANNELS = (1, 2)
SETTLE_SECONDS = 0.2   # let the output settle after a control command before reading it

# Limit Status Register bits; trip bits latch until TRIPRST
LSR_BITS = {
    0: "constant_voltage",
    1: "constant_current",
    2: "trip_overvoltage",
    3: "trip_overcurrent",
    4: "power_limit",
}


# CPX200DP hardware limits, per channel (manual Iss.8)
HW_MAX_VOLTAGE = 60.0
HW_MAX_CURRENT = 10.0
HW_MAX_POWER = 180.0
OVP_RANGE = (1.0, 66.0)      # over-voltage protection settable range, V
OCP_RANGE = (0.01, 11.0)     # over-current protection settable range, A
# PowerFlex envelope corner points (V, I_max); max current falls as V rises
_ENVELOPE_POINTS = ((0.0, 10.0), (16.0, 10.0), (35.0, 5.0), (60.0, 3.0))


def envelope_max_current(voltage):
    """Max current guaranteed at a given voltage: the lower of the corner-point
    envelope, the 180 W hyperbola, and the 10 A rail."""
    if voltage <= 0:
        return HW_MAX_CURRENT
    pts = _ENVELOPE_POINTS
    if voltage >= pts[-1][0]:
        linear = pts[-1][1]
    else:
        linear = pts[0][1]
        for (v0, i0), (v1, i1) in zip(pts, pts[1:]):
            if v0 <= voltage <= v1:
                linear = i0 + (i1 - i0) * (voltage - v0) / (v1 - v0)
                break
    return min(HW_MAX_CURRENT, linear, HW_MAX_POWER / voltage)


def _num(text):
    """Parse '5.00V', '0.123A' or 'V1 5.00' to a float."""
    token = text.strip().split()[-1]
    return float(re.sub(r"[A-Za-z]+$", "", token))


# commands carrying a setpoint or bounded arg; queries and anything unlisted
# pass through unchecked
_V_SET = re.compile(r"^\s*V([12])\s+([-\d.]+)\s*$", re.I)
_I_SET = re.compile(r"^\s*I([12])\s+([-\d.]+)\s*$", re.I)
_OVP_SET = re.compile(r"^\s*OVP([12])\s+([-\d.]+)\s*$", re.I)
_OCP_SET = re.compile(r"^\s*OCP([12])\s+([-\d.]+)\s*$", re.I)
_OP_SET = re.compile(r"^\s*OP([12])\s+(\S+)\s*$", re.I)
_OPALL_SET = re.compile(r"^\s*OPALL\s+(\S+)\s*$", re.I)
_STORE_SET = re.compile(r"^\s*(SAV|RCL)([12])\s+(\S+)\s*$", re.I)
_CONFIG_SET = re.compile(r"^\s*CONFIG\s+(\S+)\s*$", re.I)
_RATIO_SET = re.compile(r"^\s*RATIO\s+(\S+)\s*$", re.I)
_TRIPCFG_SET = re.compile(r"^\s*TRIPCONFIG\s+(\S+)\s*$", re.I)


def _as_int(token):
    """Parse an integer argument, rejecting '1.0'/'x'/'' etc."""
    if not re.fullmatch(r"[-+]?\d+", token):
        raise ValueError(f"expected an integer argument, got {token!r}")
    return int(token)


class SafetyLimits:
    """Range, envelope and argument checks on every setpoint. Rejects rather
    than clamps, so a bad run fails visibly instead of running at the wrong
    point. Operator ceilings can only tighten the hardware maxima."""

    def __init__(self, max_voltage, max_current):
        self.max_voltage = min(float(max_voltage), HW_MAX_VOLTAGE)
        self.max_current = min(float(max_current), HW_MAX_CURRENT)

    def check(self, cmd):
        m = _V_SET.match(cmd)
        if m:
            v = float(m.group(2))
            if not 0.0 <= v <= HW_MAX_VOLTAGE:
                raise ValueError(f"voltage {v}V outside instrument range "
                                 f"0-{HW_MAX_VOLTAGE:g}V (channel {m.group(1)})")
            if v > self.max_voltage:
                raise ValueError(f"voltage {v}V exceeds the safety ceiling "
                                 f"{self.max_voltage:g}V (channel {m.group(1)})")
            return
        m = _I_SET.match(cmd)
        if m:
            i = float(m.group(2))
            if not 0.0 <= i <= HW_MAX_CURRENT:
                raise ValueError(f"current {i}A outside instrument range "
                                 f"0-{HW_MAX_CURRENT:g}A (channel {m.group(1)})")
            if i > self.max_current:
                raise ValueError(f"current {i}A exceeds the safety ceiling "
                                 f"{self.max_current:g}A (channel {m.group(1)})")
            return
        m = _OVP_SET.match(cmd)
        if m:
            ovp = float(m.group(2))
            if not OVP_RANGE[0] <= ovp <= OVP_RANGE[1]:
                raise ValueError(f"OVP {ovp}V outside range "
                                 f"{OVP_RANGE[0]:g}-{OVP_RANGE[1]:g}V (channel {m.group(1)})")
            return
        m = _OCP_SET.match(cmd)
        if m:
            ocp = float(m.group(2))
            if not OCP_RANGE[0] <= ocp <= OCP_RANGE[1]:
                raise ValueError(f"OCP {ocp}A outside range "
                                 f"{OCP_RANGE[0]:g}-{OCP_RANGE[1]:g}A (channel {m.group(1)})")
            return
        m = _OP_SET.match(cmd)
        if m:
            if _as_int(m.group(2)) not in (0, 1):
                raise ValueError(f"OP{m.group(1)} takes 0 (off) or 1 (on)")
            return
        m = _OPALL_SET.match(cmd)
        if m:
            if _as_int(m.group(1)) not in (0, 1):
                raise ValueError("OPALL takes 0 (all off) or 1 (all on)")
            return
        m = _STORE_SET.match(cmd)
        if m:
            if not 0 <= _as_int(m.group(3)) <= 9:
                raise ValueError(f"{m.group(1).upper()} store must be 0-9")
            return
        m = _CONFIG_SET.match(cmd)
        if m:
            if _as_int(m.group(1)) not in (0, 2):
                raise ValueError("CONFIG takes 0 (voltage tracking) or 2 (independent)")
            return
        m = _RATIO_SET.match(cmd)
        if m:
            if not 0 <= _as_int(m.group(1)) <= 100:
                raise ValueError("RATIO must be 0-100 (percent)")
            return
        m = _TRIPCFG_SET.match(cmd)
        if m:
            if _as_int(m.group(1)) not in (0, 1):
                raise ValueError("TRIPCONFIG takes 0 (independent) or 1 (linked)")
            return

    def check_envelope(self, channel, voltage, current):
        """Reject a (V, I) operating point outside the PowerFlex envelope."""
        imax = envelope_max_current(voltage)
        if current > imax + 1e-9:
            raise ValueError(
                f"channel {channel}: {current:g}A at {voltage:g}V exceeds the "
                f"PowerFlex envelope (max {imax:.3f}A at that voltage; "
                f"{HW_MAX_POWER:g}W / {HW_MAX_CURRENT:g}A hardware limits)")


class CPX:
    """Line-based TCP driver for the CPX200DP."""

    def __init__(self, host, port, timeout=5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._buf = b""

    @property
    def connected(self):
        return self._sock is not None

    def connect(self):
        self.close()
        self._sock = socket.create_connection((self.host, self.port), self.timeout)
        self._sock.settimeout(self.timeout)
        self._buf = b""

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def send(self, cmd):
        if self._sock is None:
            raise ConnectionError("not connected to instrument")
        self._sock.sendall((cmd + "\n").encode("ascii"))

    def query(self, cmd):
        self.send(cmd)
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("instrument closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("ascii").strip()


class Monitor:
    def __init__(self, psu, interval, limits):
        self.psu = psu
        self.interval = interval
        self.limits = limits
        self.io_lock = threading.Lock()      # serialises use of the instrument socket
        self.state_lock = threading.Lock()   # guards self.snapshot
        self.snapshot = {"up": 0}
        self.poll_errors = 0
        self.idn = ""
        self.pause_until = 0.0               # set by the 'local' action

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        backoff = 1.0
        while True:
            now = time.time()
            if now < self.pause_until:
                time.sleep(min(1.0, self.pause_until - now))
                continue
            try:
                if not self.psu.connected:
                    with self.io_lock:
                        self.psu.connect()
                        self.idn = self.psu.query("*IDN?")
                    print(f"connected to {self.psu.host}:{self.psu.port} - {self.idn}", flush=True)
                self.poll_once()
                backoff = 1.0
                time.sleep(self.interval)
            except (OSError, ConnectionError, ValueError, IndexError) as exc:
                self.poll_errors += 1
                with self.state_lock:
                    self.snapshot = {"up": 0}
                self.psu.close()
                print(f"poll failed ({exc}), retrying in {backoff:.0f}s", file=sys.stderr, flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def poll_once(self):
        start = time.monotonic()
        snap = {"up": 1, "channels": {}}
        with self.io_lock:
            for ch in CHANNELS:
                lsr = int(self.psu.query(f"LSR{ch}?"))
                c = {
                    "set_voltage": _num(self.psu.query(f"V{ch}?")),
                    "set_current": _num(self.psu.query(f"I{ch}?")),
                    "output_voltage": _num(self.psu.query(f"V{ch}O?")),
                    "output_current": _num(self.psu.query(f"I{ch}O?")),
                    "output_enabled": int(self.psu.query(f"OP{ch}?")),
                    "lsr": lsr,
                }
                for bit, name in LSR_BITS.items():
                    c[name] = (lsr >> bit) & 1
                snap["channels"][ch] = c
        snap["poll_seconds"] = round(time.monotonic() - start, 4)
        snap["timestamp"] = time.time()
        with self.state_lock:
            self.snapshot = snap

    def get_snapshot(self):
        with self.state_lock:
            return dict(self.snapshot)

    def control(self, action, params):
        if not self.psu.connected:
            raise ConnectionError("exporter is not connected to the instrument")
        ch = params.get("channel", "all")

        if action == "local":
            pause = float(params.get("pause", 60))
            with self.io_lock:
                # Pause polling, else the next query re-locks the front panel.
                self.pause_until = time.time() + pause
                self.psu.send("LOCAL")
            return {"sent": ["LOCAL"], "paused_seconds": pause}

        cmds = []
        if action in ("on", "off"):
            val = 1 if action == "on" else 0
            cmds.append(f"OPALL {val}" if ch == "all" else f"OP{int(ch)} {val}")
        elif action == "set":
            if ch == "all":
                raise ValueError("set requires channel 1 or 2")
            n = int(ch)
            # envelope-check the resulting (V, I), filling the unset axis from
            # the last known setpoint
            if params.get("voltage") is not None or params.get("current") is not None:
                chan = self.get_snapshot().get("channels", {}).get(n, {})
                tgt_v = params.get("voltage", chan.get("set_voltage"))
                tgt_i = params.get("current", chan.get("set_current"))
                if tgt_v is not None and tgt_i is not None:
                    self.limits.check_envelope(n, float(tgt_v), float(tgt_i))
            # round to hardware resolution (V 10mV, I 1mA, OVP 100mV, OCP 10mA)
            for key, prefix, fmt in (("voltage", "V", "{:.2f}"), ("current", "I", "{:.3f}"),
                                     ("ovp", "OVP", "{:.1f}"), ("ocp", "OCP", "{:.2f}")):
                if params.get(key) is not None:
                    cmds.append(f"{prefix}{n} " + fmt.format(float(params[key])))
            if not cmds:
                raise ValueError("set requires at least one of voltage/current/ovp/ocp")
        elif action == "triprst":
            cmds.append("TRIPRST")
        else:
            raise ValueError(f"unknown action {action!r}")

        for cmd in cmds:                 # reject before sending anything
            self.limits.check(cmd)
        with self.io_lock:
            self.pause_until = 0.0
            for cmd in cmds:
                self.psu.send(cmd)
            self.psu.query("*OPC?")  # wait for completion
        time.sleep(SETTLE_SECONDS)   # output settles before we read it back
        self.poll_once()
        return {"sent": cmds, "state": self.get_snapshot()}

    def scpi(self, write=None, ask=None):
        """Raw SCPI passthrough for experiments, on the polling lock."""
        if not self.psu.connected:
            raise ConnectionError("exporter is not connected to the instrument")
        cmd = ask if ask is not None else write
        if cmd is None:
            raise ValueError("provide 'write' (no reply) or 'ask' (expect a reply)")
        self.limits.check(cmd)
        # envelope-check a raw V/I write against the channel's other axis
        mv, mi = _V_SET.match(cmd), _I_SET.match(cmd)
        if mv or mi:
            n = int((mv or mi).group(1))
            chan = self.get_snapshot().get("channels", {}).get(n, {})
            if mv and chan.get("set_current") is not None:
                self.limits.check_envelope(n, float(mv.group(2)), float(chan["set_current"]))
            elif mi and chan.get("set_voltage") is not None:
                self.limits.check_envelope(n, float(chan["set_voltage"]), float(mi.group(2)))
        with self.io_lock:
            self.pause_until = 0.0
            if ask is not None:
                return {"reply": self.psu.query(ask)}
            self.psu.send(write)
            return {"ok": True, "sent": write}


def render_metrics(mon):
    snap = mon.get_snapshot()
    out = []

    def metric(name, help_text, mtype, samples):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {mtype}")
        for labels, value in samples:
            out.append(f"{name}{labels} {value}")

    metric("cpx_up", "1 if the last poll of the instrument succeeded", "gauge",
           [("", snap.get("up", 0))])
    metric("cpx_poll_errors_total", "Failed polls since the exporter started", "counter",
           [("", mon.poll_errors)])
    metric("cpx_polling_paused", "1 while polling is paused (front panel handed back to local)",
           "gauge", [("", 1 if time.time() < mon.pause_until else 0)])
    metric("cpx_max_voltage_volts", "Configured voltage safety ceiling", "gauge",
           [("", mon.limits.max_voltage)])
    metric("cpx_max_current_amps", "Configured current safety ceiling", "gauge",
           [("", mon.limits.max_current)])
    if mon.idn:
        idn = mon.idn.replace("\\", "\\\\").replace('"', '\\"')
        metric("cpx_info", "Instrument identity string", "gauge", [(f'{{idn="{idn}"}}', 1)])

    chans = snap.get("channels", {})
    per_channel = [
        ("cpx_set_voltage_volts", "Voltage setpoint", "set_voltage"),
        ("cpx_set_current_amps", "Current limit setpoint", "set_current"),
        ("cpx_output_voltage_volts", "Measured output voltage", "output_voltage"),
        ("cpx_output_current_amps", "Measured output current", "output_current"),
        ("cpx_output_enabled", "1 if the output is switched on", "output_enabled"),
        ("cpx_limit_status_register", "Raw Limit Status Register value", "lsr"),
    ] + [(f"cpx_{name}", f"LSR bit {bit}: {name}", name) for bit, name in LSR_BITS.items()]
    for mname, help_text, key in per_channel:
        samples = [(f'{{channel="{ch}"}}', c[key]) for ch, c in sorted(chans.items())]
        if samples:
            metric(mname, help_text, "gauge", samples)
    if chans:
        metric("cpx_output_power_watts", "Measured output power (V*I)", "gauge",
               [(f'{{channel="{ch}"}}', round(c["output_voltage"] * c["output_current"], 4))
                for ch, c in sorted(chans.items())])
    if "poll_seconds" in snap:
        metric("cpx_poll_duration_seconds", "Time the last instrument poll took", "gauge",
               [("", snap["poll_seconds"])])
    return "\n".join(out) + "\n"


def make_handler(mon, allow_remote_control):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body, ctype="text/plain; charset=utf-8"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/metrics":
                self._reply(200, render_metrics(mon),
                            "text/plain; version=0.0.4; charset=utf-8")
            elif self.path == "/status":
                self._reply(200, json.dumps({"idn": mon.idn, **mon.get_snapshot()}, indent=2),
                            "application/json")
            elif self.path == "/":
                self._reply(200, "cpx-exporter: GET /metrics, GET /status, "
                                 "POST /control, POST /scpi\n")
            else:
                self._reply(404, "not found\n")

        def do_POST(self):
            # Read the body first so the connection stays in sync on reject.
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length) or b"{}")
            except ValueError as exc:
                self._reply(400, f"bad request: {exc}\n")
                return

            is_local = self.client_address[0] in ("127.0.0.1", "::1")
            if self.path == "/control":
                if not allow_remote_control and not is_local:
                    self._reply(403, "control is localhost-only: ssh in and use psuctl\n")
                    return
                run = lambda: mon.control(req.get("action", ""), req)
            elif self.path == "/scpi":
                if not is_local:
                    self._reply(403, "/scpi is localhost-only\n")
                    return
                run = lambda: mon.scpi(write=req.get("write"), ask=req.get("ask"))
            else:
                self._reply(404, "not found\n")
                return

            try:
                self._reply(200, json.dumps(run(), indent=2), "application/json")
            except ValueError as exc:
                self._reply(400, f"bad request: {exc}\n")
            except (OSError, ConnectionError) as exc:
                self._reply(503, f"instrument unavailable: {exc}\n")

        def log_message(self, fmt, *args):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # defaults come from .env (see config.py); flags override for one-off runs
    ap.add_argument("--psu-host", default=config.PSU_HOST, help="hostname/IP of the CPX200DP")
    ap.add_argument("--psu-port", type=int, default=config.PSU_PORT)
    ap.add_argument("--listen", default=config.LISTEN, help="HTTP bind address")
    ap.add_argument("--port", type=int, default=config.HTTP_PORT, help="HTTP port for /metrics")
    ap.add_argument("--interval", type=float, default=config.POLL_INTERVAL, help="poll interval [s]")
    ap.add_argument("--max-voltage", type=float, default=config.MAX_VOLTAGE,
                    help="voltage ceiling [V]; set to what the load can bear")
    ap.add_argument("--max-current", type=float, default=config.MAX_CURRENT,
                    help="current ceiling [A]; set to what the load can bear")
    ap.add_argument("--allow-remote-control", action="store_true",
                    help="allow POST /control from other hosts (default: localhost)")
    args = ap.parse_args()

    limits = SafetyLimits(args.max_voltage, args.max_current)
    mon = Monitor(CPX(args.psu_host, args.psu_port), args.interval, limits)
    mon.start()
    server = HTTPServer((args.listen, args.port),
                        make_handler(mon, args.allow_remote_control))
    print(f"listening on {args.listen}:{args.port}, "
          f"polling {args.psu_host}:{args.psu_port} every {args.interval}s "
          f"(safety limits: {args.max_voltage}V / {args.max_current}A)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
