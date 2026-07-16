#!/usr/bin/env python3
"""Write/control check for the CPX200DP, through the exporter.

Writes tiny setpoints (1.00 V / 0.10 A) to a channel, verifies they took, then
restores the original setpoints. It only ever sends V/I, so it never changes
the output on/off state (verified). It refuses a channel whose original setpoint
is outside the envelope, since it could not be put back.

Besides the setpoint (V{ch}?, what the instrument was told to store), it also
checks the measured terminal voltage (V{ch}O?, what the meter reads at the
rail) so the test proves the physical rail actually moved, not just that the
command landed. The rail only tracks the setpoint when the output is energized
and in constant-voltage mode (no load pinning it to the current limit), so the
rail check runs only on a live channel and is skipped-with-note on an off one.

Note: it does not require the output to be off, so on a live channel the
terminal voltage dips to the test value during the run and is restored after.

    python3 tests/write_check.py             # first channel
    python3 tests/write_check.py --channel 2
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from exporter import envelope_max_current, HW_MAX_VOLTAGE, HW_MAX_CURRENT

TEST_V = 1.00
TEST_I = 0.10
TOL = 0.05          # setpoint readback tolerance (V{ch}? vs commanded)
MEAS_TOL = 0.20     # measured rail tolerance (V{ch}O? vs setpoint), looser
SETTLE_TIMEOUT = 6.0  # a falling rail into open terminals bleeds down slowly

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


def _get(path):
    with urllib.request.urlopen(config.EXPORTER_URL + path, timeout=15) as r:
        return json.loads(r.read())


def _set(ch, v, i):
    data = json.dumps({"action": "set", "channel": str(ch),
                       "voltage": v, "current": i}).encode()
    req = urllib.request.Request(config.EXPORTER_URL + "/control", data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def chan(status, ch):
    return (status.get("channels") or {}).get(str(ch))


def within_envelope(v, i):
    return (0 <= v <= HW_MAX_VOLTAGE and 0 <= i <= HW_MAX_CURRENT
            and i <= envelope_max_current(v) + 1e-9)


def settle_rail(ch, target, tol=MEAS_TOL, timeout=SETTLE_TIMEOUT, interval=0.3):
    """Poll measured output voltage until it lands within tol of target (or the
    timeout elapses), then return the last channel snapshot. Lets a slewing rail
    settle before we assert on it."""
    c = chan(_get("/status"), ch)
    deadline = time.time() + timeout
    while time.time() < deadline and abs((c.get("output_voltage") or 0.0) - target) > tol:
        time.sleep(interval)
        c = chan(_get("/status"), ch)
    return c


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", type=int, choices=[1, 2], help="force a channel")
    args = ap.parse_args()

    try:
        status = _get("/status")
    except OSError as exc:
        print(f"cannot reach exporter at {config.EXPORTER_URL} ({exc})")
        return 1
    if status.get("up") != 1:
        print("exporter is not connected to the PSU; aborting")
        return 1

    print("current state:")
    for cid in sorted(status.get("channels") or {}):
        c = status["channels"][cid]
        print(f"  ch{cid}: output={'ON' if c['output_enabled'] else 'off'} "
              f"V_set={c['set_voltage']} I_set={c['set_current']}")

    target = None
    for ch in ([args.channel] if args.channel else [1, 2]):
        c = chan(status, ch)
        if c is None:
            continue
        if not within_envelope(c["set_voltage"], c["set_current"]):
            print(f"ch{ch}: original setpoint outside the envelope - skipping "
                  "(could not restore it)")
            continue
        target = ch
        break

    if target is None:
        print("\nno channel with an in-envelope setpoint to write to. "
              "Nothing was written.")
        return 1

    orig = chan(status, target)
    orig_v, orig_i, orig_out = orig["set_voltage"], orig["set_current"], orig["output_enabled"]
    print(f"\n== write check on ch{target} (output {'ON' if orig_out else 'off'}; "
          f"original V={orig_v} I={orig_i}) ==")

    wrote = False
    try:
        _set(target, TEST_V, TEST_I)
        wrote = True
        c = chan(_get("/status"), target)
        check(f"ch{target}: output state unchanged after write",
              c["output_enabled"] == orig_out, c["output_enabled"])
        check(f"ch{target}: voltage set to {TEST_V}", abs(c["set_voltage"] - TEST_V) < TOL,
              c["set_voltage"])
        check(f"ch{target}: current set to {TEST_I}", abs(c["set_current"] - TEST_I) < TOL,
              c["set_current"])
        if orig_out:
            c = settle_rail(target, TEST_V)
            check(f"ch{target}: measured rail moved to ~{TEST_V} V",
                  abs(c["output_voltage"] - TEST_V) < MEAS_TOL, f"V_out={c['output_voltage']}")
        else:
            print(f"  SKIP  ch{target}: output off - rail reads ~0 V, movement not observable")
    except urllib.error.HTTPError as exc:
        check(f"ch{target}: tiny setpoint accepted", False, exc.read().decode().strip())
    finally:
        if wrote:
            try:
                _set(target, orig_v, orig_i)
                c = chan(_get("/status"), target)
                check(f"ch{target}: voltage restored to {orig_v}",
                      abs(c["set_voltage"] - orig_v) < TOL, c["set_voltage"])
                check(f"ch{target}: current restored to {orig_i}",
                      abs(c["set_current"] - orig_i) < TOL, c["set_current"])
                check(f"ch{target}: output state unchanged at end",
                      c["output_enabled"] == orig_out, c["output_enabled"])
                if orig_out:
                    c = settle_rail(target, orig_v)
                    check(f"ch{target}: measured rail restored to ~{orig_v} V",
                          abs(c["output_voltage"] - orig_v) < MEAS_TOL, f"V_out={c['output_voltage']}")
            except Exception as exc:
                check(f"ch{target}: restore original setpoints", False, exc)
                print(f"  !! ch{target} may still be at {TEST_V} V / {TEST_I} A. "
                      f"Restore manually: V={orig_v} I={orig_i}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
