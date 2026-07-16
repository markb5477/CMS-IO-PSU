#!/usr/bin/env python3
"""Read-only health checks for the CPX200DP. Sends only SCPI queries (every
command ends in '?'), so it never changes a setpoint or the output state - safe
to run on live hardware.

  python3 tests/readonly.py            # via the exporter's read-only HTTP
  python3 tests/readonly.py --direct   # straight to the PSU over TCP

The instrument accepts one TCP client at a time, so use --direct only when the
exporter service is stopped; otherwise go through the exporter.

Besides checking each readback parses, it cross-checks the measured rail
(output_voltage) against the setpoint and output state, so the measured values
are proven physically consistent, not merely numeric: an off channel's rail
should be collapsed (~0 V); a live constant-voltage channel's rail should track
its setpoint; a current-limited channel legitimately sits below setpoint and is
skipped. All still query-only.
"""

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

MEAS_TOL = 0.20     # measured rail vs setpoint tolerance (V), CV mode
RAIL_OFF_MAX = 0.5  # an off channel's terminals should read below this (V)
CC_MARGIN = 0.05    # I_out within this of I_set counts as current-limited (A)

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))
    return ok


def is_number(x):
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def check_channel(name, ch):
    for key in ("set_voltage", "set_current", "output_voltage", "output_current"):
        check(f"{name}: {key} numeric", is_number(ch.get(key)), ch.get(key))
    check(f"{name}: output_enabled is 0/1", ch.get("output_enabled") in (0, 1),
          ch.get("output_enabled"))
    lsr = ch.get("lsr")
    check(f"{name}: LSR in 0..31", isinstance(lsr, int) and 0 <= lsr <= 31, lsr)


def check_rail_consistency(name, ch):
    """Cross-check the measured rail against the setpoint and output state, so
    output_voltage is proven to reflect physical reality - not just parse."""
    en = ch.get("output_enabled")
    vals = [ch.get(k) for k in ("set_voltage", "output_voltage", "set_current",
                                "output_current")]
    if en not in (0, 1) or not all(is_number(x) for x in vals):
        return  # the numeric/output_enabled checks already flagged this
    v_set, v_out, i_set, i_out = (float(x) for x in vals)
    if en == 0:
        check(f"{name}: rail collapsed with output off (V_out~0)",
              v_out < RAIL_OFF_MAX, f"V_out={v_out}")
    elif i_out < i_set - CC_MARGIN:  # constant-voltage: rail holds the setpoint
        check(f"{name}: measured rail tracks setpoint (CV)",
              abs(v_out - v_set) < MEAS_TOL, f"V_out={v_out} V_set={v_set}")
    else:  # current-limited: rail legitimately sits below the setpoint
        print(f"  SKIP  {name}: current-limited (I_out={i_out}~I_set={i_set}), "
              "rail below setpoint by design")


def via_exporter(url):
    print(f"== read-only via exporter {url} ==")
    try:
        with urllib.request.urlopen(url + "/status", timeout=10) as r:
            status = json.loads(r.read())
        with urllib.request.urlopen(url + "/metrics", timeout=10) as r:
            metrics = r.read().decode()
    except OSError as exc:
        check("reach exporter", False, f"{exc}; is cpx-exporter running?")
        return

    check("exporter reports up", status.get("up") == 1, status.get("up"))
    check("IDN is a CPX200DP", "CPX200DP" in (status.get("idn") or ""), status.get("idn"))
    chans = status.get("channels") or {}
    check("both channels present", {"1", "2"}.issubset(chans), list(chans))
    for cid in sorted(chans):
        check_channel(f"ch{cid}", chans[cid])
        check_rail_consistency(f"ch{cid}", chans[cid])
    check("/metrics exposes cpx_up 1", "\ncpx_up 1" in ("\n" + metrics))
    check("/metrics has an output-voltage sample", "cpx_output_voltage_volts{" in metrics)


def direct(host, port):
    from exporter import CPX, _num  # reuse the tested driver + parser
    print(f"== read-only direct to {host}:{port} ==")
    psu = CPX(host, port)
    try:
        psu.connect()
    except OSError as exc:
        check("connect to instrument", False, exc)
        return

    def q(cmd):
        assert cmd.endswith("?"), f"not a query, refusing to send: {cmd}"
        return psu.query(cmd)

    try:
        check("IDN is a CPX200DP", "CPX200DP" in q("*IDN?"))
        for ch in (1, 2):
            try:
                data = {
                    "set_voltage": _num(q(f"V{ch}?")),
                    "set_current": _num(q(f"I{ch}?")),
                    "output_voltage": _num(q(f"V{ch}O?")),
                    "output_current": _num(q(f"I{ch}O?")),
                    "output_enabled": int(q(f"OP{ch}?")),
                    "lsr": int(q(f"LSR{ch}?")),
                }
            except (ValueError, IndexError, OSError) as exc:
                check(f"ch{ch}: readbacks parse", False, exc)
                continue
            check_channel(f"ch{ch}", data)
            check_rail_consistency(f"ch{ch}", data)
    finally:
        psu.close()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--direct", action="store_true",
                    help="query the instrument directly (exporter must be stopped)")
    ap.add_argument("--host", default=config.PSU_HOST)
    ap.add_argument("--port", type=int, default=config.PSU_PORT)
    ap.add_argument("--url", default=config.EXPORTER_URL)
    args = ap.parse_args()

    if args.direct:
        direct(args.host, args.port)
    else:
        via_exporter(args.url)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
