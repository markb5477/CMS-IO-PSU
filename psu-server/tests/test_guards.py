#!/usr/bin/env python3
"""Hardware-free tests for every PSU command path and safety guard.

Unlike readonly.py / write_check.py (which need a live instrument), this suite
injects a fake CPX (tests/fakepsu.py) into the real exporter code, so it never
opens a socket or touches PSU_HOST. It is safe to run anywhere (it never opens a socket to PSU_HOST):

    cd psu-server && python3 -m unittest discover -s tests -v
    cd psu-server && python3 -m unittest tests.test_guards -v   # or explicitly

It covers:
  * envelope_max_current / check_envelope   (the PowerFlex operating envelope)
  * SafetyLimits.check                        (range/ceiling/argument checks per
                                               command family, accept + reject)
  * _num / _as_int                            (parsers the checks rely on)
  * Monitor.control                           (on/off/set/triprst/local -> SCPI,
                                               reject-before-send, envelope fill)
  * Monitor.scpi                              (raw passthrough + its guards)
  * the HTTP handler                          (localhost-only auth, status codes)
"""

import email.message
import io
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))   # psu-server/, for config + exporter
sys.path.insert(0, _HERE)                    # tests/, for fakepsu

import exporter
from exporter import (
    Monitor, SafetyLimits, envelope_max_current, make_handler,
    _num, _as_int, HW_MAX_VOLTAGE, HW_MAX_CURRENT,
)
from fakepsu import FakeCPX, IDN


def full_limits():
    return SafetyLimits(HW_MAX_VOLTAGE, HW_MAX_CURRENT)


# --------------------------------------------------------------------------- #
# Operating envelope
# --------------------------------------------------------------------------- #
class TestEnvelope(unittest.TestCase):
    def test_corner_points(self):
        self.assertAlmostEqual(envelope_max_current(0.0), 10.0)
        self.assertAlmostEqual(envelope_max_current(16.0), 10.0)
        self.assertAlmostEqual(envelope_max_current(35.0), 5.0)
        self.assertAlmostEqual(envelope_max_current(60.0), 3.0)

    def test_negative_and_zero_voltage_give_full_rail(self):
        self.assertEqual(envelope_max_current(-5.0), HW_MAX_CURRENT)
        self.assertEqual(envelope_max_current(0.0), HW_MAX_CURRENT)

    def test_linear_interpolation_between_corners(self):
        # halfway from (35,5) to (60,3): 47.5V -> 4.0A on the linear leg, but the
        # 180W hyperbola (180/47.5 = 3.79A) is lower, so it wins.
        self.assertAlmostEqual(envelope_max_current(47.5), 180.0 / 47.5, places=6)

    def test_power_hyperbola_dominates_midrange(self):
        # at 25V the linear leg allows ~7.63A but the 180W hyperbola (7.2A) is
        # lower, so power wins.
        self.assertAlmostEqual(envelope_max_current(25.0), 180.0 / 25.0, places=6)

    def test_above_max_voltage_clamps_to_last_corner_or_power(self):
        # >=60V: linear leg holds 3A, power limit 180/60 = 3A -> 3A
        self.assertAlmostEqual(envelope_max_current(60.0), 3.0)
        self.assertLessEqual(envelope_max_current(66.0), 3.0)

    def test_monotonically_non_increasing(self):
        prev = envelope_max_current(0.1)
        v = 0.2
        while v <= 60.0:
            cur = envelope_max_current(v)
            self.assertLessEqual(cur, prev + 1e-9, f"envelope rose at {v}V")
            prev = cur
            v += 0.1

    def test_check_envelope_accepts_inside(self):
        full_limits().check_envelope(1, 12.0, 5.0)      # well inside
        full_limits().check_envelope(1, 60.0, 3.0)      # exactly on the corner

    def test_check_envelope_rejects_outside(self):
        with self.assertRaises(ValueError):
            full_limits().check_envelope(1, 60.0, 5.0)  # 5A at 60V is over 180W


# --------------------------------------------------------------------------- #
# SafetyLimits.check - the per-command guard
# --------------------------------------------------------------------------- #
class TestSafetyCheck(unittest.TestCase):
    def setUp(self):
        self.lim = full_limits()

    def ok(self, cmd):
        self.lim.check(cmd)  # must not raise

    def bad(self, cmd):
        with self.assertRaises(ValueError, msg=f"expected {cmd!r} to be rejected"):
            self.lim.check(cmd)

    def test_voltage_range(self):
        self.ok("V1 5.0")
        self.ok("V2 0")
        self.ok("V1 60")
        self.bad("V1 -0.1")
        self.bad("V1 60.1")

    def test_current_range(self):
        self.ok("I1 0.5")
        self.ok("I2 10")
        self.bad("I1 -1")
        self.bad("I1 10.1")

    def test_safety_ceiling_tightens_below_hardware_max(self):
        lim = SafetyLimits(12.0, 2.0)
        lim.check("V1 12")          # at the ceiling: allowed
        lim.check("I1 2")
        with self.assertRaises(ValueError):
            lim.check("V1 12.5")    # within hardware range but over the ceiling
        with self.assertRaises(ValueError):
            lim.check("I1 2.5")

    def test_ceiling_cannot_exceed_hardware(self):
        lim = SafetyLimits(999, 999)
        self.assertEqual(lim.max_voltage, HW_MAX_VOLTAGE)
        self.assertEqual(lim.max_current, HW_MAX_CURRENT)

    def test_ovp_ocp_range(self):
        self.ok("OVP1 5")
        self.bad("OVP1 0.5")        # below OVP_RANGE
        self.bad("OVP1 70")
        self.ok("OCP1 1.0")
        self.bad("OCP1 0")          # below OCP_RANGE
        self.bad("OCP1 12")

    def test_output_switch_args(self):
        self.ok("OP1 0")
        self.ok("OP2 1")
        self.bad("OP1 2")
        self.bad("OP1 1.0")         # not an integer
        self.bad("OP1 on")
        self.ok("OPALL 1")
        self.bad("OPALL 2")

    def test_store_recall_range(self):
        self.ok("SAV1 0")
        self.ok("RCL2 9")
        self.bad("SAV1 10")
        self.bad("RCL1 -1")

    def test_config_ratio_tripconfig(self):
        self.ok("CONFIG 0")
        self.ok("CONFIG 2")
        self.bad("CONFIG 1")
        self.ok("RATIO 50")
        self.bad("RATIO 101")
        self.ok("TRIPCONFIG 1")
        self.bad("TRIPCONFIG 2")

    def test_queries_and_unlisted_pass_through(self):
        # the guard only vets bounded setpoints; queries and unlisted commands
        # are not its job and must pass untouched.
        for cmd in ("V1?", "*IDN?", "LSR1?", "TRIPRST", "LOCAL", "*OPC?"):
            self.ok(cmd)

    def test_case_insensitive(self):
        self.ok("v1 5.0")
        self.bad("v1 61")


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
class TestParsers(unittest.TestCase):
    def test_num(self):
        self.assertAlmostEqual(_num("5.00V"), 5.00)
        self.assertAlmostEqual(_num("0.123A"), 0.123)
        self.assertAlmostEqual(_num("V1 5.00"), 5.00)
        self.assertAlmostEqual(_num("  -1.5  "), -1.5)

    def test_as_int(self):
        self.assertEqual(_as_int("0"), 0)
        self.assertEqual(_as_int("-3"), -3)
        self.assertEqual(_as_int("+7"), 7)
        for bad in ("1.0", "x", "", "1e3", " "):
            with self.assertRaises(ValueError):
                _as_int(bad)


# --------------------------------------------------------------------------- #
# Monitor.control - action -> SCPI command construction and guards
# --------------------------------------------------------------------------- #
class TestControl(unittest.TestCase):
    def setUp(self):
        self.psu = FakeCPX()
        self.psu.connect()
        self.mon = Monitor(self.psu, interval=1.0, limits=full_limits())
        self.mon.idn = IDN
        self.mon.poll_once()          # populate the snapshot control() reads
        self.psu.sent.clear()

    def writes(self):
        """Commands that reached the wire, excluding the *OPC? sync query."""
        return [c for c in self.psu.sent if c != "*OPC?"]

    def test_on_single_channel(self):
        res = self.mon.control("on", {"channel": "1"})
        self.assertEqual(res["sent"], ["OP1 1"])
        self.assertIn("OP1 1", self.writes())
        self.assertEqual(self.psu.ch[1]["op"], 1)

    def test_on_all(self):
        self.mon.control("on", {"channel": "all"})
        self.assertIn("OPALL 1", self.writes())
        self.assertTrue(all(c["op"] == 1 for c in self.psu.ch.values()))

    def test_off_all(self):
        self.psu.ch[1]["op"] = self.psu.ch[2]["op"] = 1
        self.mon.control("off", {"channel": "all"})
        self.assertIn("OPALL 0", self.writes())
        self.assertTrue(all(c["op"] == 0 for c in self.psu.ch.values()))

    def test_set_voltage_and_current_rounding_and_order(self):
        res = self.mon.control("set", {"channel": "1", "voltage": 5.0, "current": 0.5})
        # voltage before current, rounded to hardware resolution
        self.assertEqual(res["sent"], ["V1 5.00", "I1 0.500"])

    def test_set_ovp_ocp_formatting(self):
        res = self.mon.control("set", {"channel": "2", "ovp": 12.34, "ocp": 1.234})
        self.assertEqual(res["sent"], ["OVP2 12.3", "OCP2 1.23"])

    def test_set_requires_a_field(self):
        with self.assertRaises(ValueError):
            self.mon.control("set", {"channel": "1"})
        self.assertEqual(self.writes(), [])

    def test_set_rejects_channel_all(self):
        with self.assertRaises(ValueError):
            self.mon.control("set", {"channel": "all", "voltage": 5.0})

    def test_unknown_action(self):
        with self.assertRaises(ValueError):
            self.mon.control("explode", {"channel": "1"})

    def test_triprst(self):
        res = self.mon.control("triprst", {})
        self.assertEqual(res["sent"], ["TRIPRST"])

    def test_local_pauses_polling_and_does_not_send_setpoints(self):
        res = self.mon.control("local", {"pause": 30})
        self.assertEqual(res["paused_seconds"], 30)
        self.assertIn("LOCAL", self.psu.sent)
        self.assertGreater(self.mon.pause_until, 0)

    def test_ceiling_rejects_before_sending_anything(self):
        mon = Monitor(self.psu, 1.0, SafetyLimits(12.0, 2.0))
        mon.poll_once()
        self.psu.sent.clear()
        with self.assertRaises(ValueError):
            mon.control("set", {"channel": "1", "voltage": 20.0})
        self.assertEqual(self.writes(), [], "no command may be sent when a setpoint is rejected")

    def test_envelope_fills_unset_axis_from_snapshot(self):
        # park ch1 at 60V, then ask for 5A alone: 5A at 60V is over the 180W
        # envelope, and control() must catch it using the stored voltage.
        self.psu.ch[1]["v"] = 60.0
        self.mon.poll_once()
        self.psu.sent.clear()
        with self.assertRaises(ValueError):
            self.mon.control("set", {"channel": "1", "current": 5.0})
        self.assertEqual(self.writes(), [])

    def test_envelope_allows_inside_point(self):
        self.psu.ch[1]["v"] = 12.0
        self.mon.poll_once()
        self.psu.sent.clear()
        res = self.mon.control("set", {"channel": "1", "current": 5.0})
        self.assertEqual(res["sent"], ["I1 5.000"])

    def test_control_when_disconnected_raises(self):
        self.psu.close()
        with self.assertRaises(ConnectionError):
            self.mon.control("on", {"channel": "1"})


# --------------------------------------------------------------------------- #
# Monitor.scpi - raw passthrough for experiments
# --------------------------------------------------------------------------- #
class TestScpi(unittest.TestCase):
    def setUp(self):
        self.psu = FakeCPX()
        self.psu.connect()
        self.mon = Monitor(self.psu, 1.0, full_limits())
        self.mon.poll_once()
        self.psu.sent.clear()

    def test_ask_returns_reply(self):
        self.assertEqual(self.mon.scpi(ask="*IDN?"), {"reply": IDN})

    def test_write_ok(self):
        res = self.mon.scpi(write="OP1 1")
        self.assertEqual(res, {"ok": True, "sent": "OP1 1"})
        self.assertEqual(self.psu.ch[1]["op"], 1)

    def test_requires_write_or_ask(self):
        with self.assertRaises(ValueError):
            self.mon.scpi()

    def test_guard_rejects_out_of_range_write(self):
        with self.assertRaises(ValueError):
            self.mon.scpi(write="V1 61")
        self.assertNotIn("V1 61", self.psu.sent)

    def test_envelope_checked_on_raw_write(self):
        self.psu.ch[1]["i"] = 5.0     # stored current axis
        self.mon.poll_once()
        with self.assertRaises(ValueError):
            self.mon.scpi(write="V1 60")   # 5A at 60V is over the envelope
        self.assertNotIn("V1 60", self.psu.sent)

    def test_disconnected_raises(self):
        self.psu.close()
        with self.assertRaises(ConnectionError):
            self.mon.scpi(ask="*IDN?")


# --------------------------------------------------------------------------- #
# HTTP handler - the network-facing guards and status codes
# --------------------------------------------------------------------------- #
class HandlerHarness:
    """Drive a Handler instance without a socket: build it with __new__, wire up
    fake rfile/wfile/headers/client_address, call the method, parse the reply."""

    def __init__(self, mon, allow_remote_control):
        self.Handler = make_handler(mon, allow_remote_control)

    def request(self, method, path, body=None, client="127.0.0.1"):
        h = self.Handler.__new__(self.Handler)
        h.client_address = (client, 40000)
        h.request_version = "HTTP/1.1"
        h.requestline = f"{method} {path} HTTP/1.1"  # read by log_request
        h.command = method
        h.path = path
        raw = b"" if body is None else json.dumps(body).encode()
        h.headers = email.message.Message()
        h.headers["Content-Length"] = str(len(raw))
        h.rfile = io.BytesIO(raw)
        h.wfile = io.BytesIO()
        (h.do_GET if method == "GET" else h.do_POST)()
        return self._parse(h.wfile.getvalue())

    @staticmethod
    def _parse(raw):
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        return status, body.decode()


def connected_mon(**kw):
    psu = FakeCPX()
    psu.connect()
    mon = Monitor(psu, 1.0, full_limits())
    mon.idn = IDN
    mon.poll_once()
    return mon


class TestHttpGuards(unittest.TestCase):
    def test_metrics_and_status_and_root(self):
        h = HandlerHarness(connected_mon(), allow_remote_control=False)
        code, body = h.request("GET", "/metrics")
        self.assertEqual(code, 200)
        self.assertIn("cpx_up 1", body)
        code, body = h.request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertIn("CPX200DP", body)
        self.assertEqual(h.request("GET", "/")[0], 200)

    def test_unknown_path_404(self):
        h = HandlerHarness(connected_mon(), allow_remote_control=False)
        self.assertEqual(h.request("GET", "/nope")[0], 404)
        self.assertEqual(h.request("POST", "/nope", {})[0], 404)

    def test_control_allowed_from_localhost(self):
        h = HandlerHarness(connected_mon(), allow_remote_control=False)
        code, _ = h.request("POST", "/control", {"action": "on", "channel": "1"},
                            client="127.0.0.1")
        self.assertEqual(code, 200)

    def test_control_blocked_from_remote_by_default(self):
        h = HandlerHarness(connected_mon(), allow_remote_control=False)
        code, body = h.request("POST", "/control", {"action": "on", "channel": "1"},
                               client="10.0.0.5")
        self.assertEqual(code, 403)
        self.assertIn("localhost-only", body)

    def test_control_allowed_from_remote_when_opted_in(self):
        h = HandlerHarness(connected_mon(), allow_remote_control=True)
        code, _ = h.request("POST", "/control", {"action": "on", "channel": "1"},
                            client="10.0.0.5")
        self.assertEqual(code, 200)

    def test_scpi_is_always_localhost_only(self):
        # even with remote control opted in, /scpi stays local-only
        h = HandlerHarness(connected_mon(), allow_remote_control=True)
        self.assertEqual(
            h.request("POST", "/scpi", {"ask": "*IDN?"}, client="10.0.0.5")[0], 403)
        self.assertEqual(
            h.request("POST", "/scpi", {"ask": "*IDN?"}, client="127.0.0.1")[0], 200)

    def test_bad_json_is_400(self):
        h = HandlerHarness(connected_mon(), allow_remote_control=False)
        # hand-craft a body that is not valid JSON
        H = h.Handler.__new__(h.Handler)
        H.client_address = ("127.0.0.1", 40000)
        H.request_version = "HTTP/1.1"
        H.requestline = "POST /control HTTP/1.1"
        H.command = "POST"
        H.path = "/control"
        raw = b"{not json"
        H.headers = email.message.Message()
        H.headers["Content-Length"] = str(len(raw))
        H.rfile = io.BytesIO(raw)
        H.wfile = io.BytesIO()
        H.do_POST()
        code, _ = HandlerHarness._parse(H.wfile.getvalue())
        self.assertEqual(code, 400)

    def test_rejected_setpoint_is_400(self):
        h = HandlerHarness(connected_mon(), allow_remote_control=False)
        code, body = h.request("POST", "/control",
                               {"action": "set", "channel": "1", "voltage": 999})
        self.assertEqual(code, 400)

    def test_instrument_unavailable_is_503(self):
        psu = FakeCPX()  # never connected
        mon = Monitor(psu, 1.0, full_limits())
        h = HandlerHarness(mon, allow_remote_control=False)
        code, body = h.request("POST", "/control", {"action": "on", "channel": "1"})
        self.assertEqual(code, 503)
        self.assertIn("unavailable", body)


if __name__ == "__main__":
    unittest.main()
