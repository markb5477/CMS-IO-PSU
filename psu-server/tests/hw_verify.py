#!/usr/bin/env python3
"""Real-hardware verification of every PSU command and guard, through the live
exporter, holding each applied setpoint long enough (~10 s) to register in
Prometheus/Grafana so the whole run is recorded.

Unlike tests/test_guards.py (which proves the same logic in software against a
fake instrument), this drives the real CPX200DP. It is built defensively for a
run with the detector connected:

  * REQUIRES the exporter's own safety ceiling to be <= the run cap
    (default 12 V / 1 A) so the exporter itself refuses anything higher - a
    hardware backstop independent of this script. Restart the exporter with
    --max-voltage 12 --max-current 1 (or set them in .env) first, or pass
    --allow-high-ceiling to override.
  * Programs with outputs OFF, sets a low start voltage BEFORE the OVP, and
    trip-resets - so lowering OVP never latches a trip (CPX gotcha).
  * Sets an OVP/OCP hardware backstop just above the top of the run.
  * Ramps voltage up and back down in gentle steps, never above the cap.
  * The guard-rejection checks are inherently safe: the exporter rejects them
    before anything reaches the instrument, so nothing is energized by them.
  * On normal exit, abort, Ctrl-C or a trip it ALWAYS turns both outputs off
    and restores the original setpoints / OVP / OCP.

    # on the lab PC, exporter already running with a 12 V / 1 A ceiling:
    python3 tests/hw_verify.py                 # full recorded run on channel 1
    python3 tests/hw_verify.py --channel 2 --dwell 10 --out run.csv
    python3 tests/hw_verify.py --dry-run       # guards + readback only, NO energizing
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from exporter import _num  # reuse the instrument-reply parser

TURN_ON_SETTLE = 0.5      # ride out the CPX turn-on OVP transient before reading LSR
TRIP_DEBOUNCE = 0.2       # a real trip latches; a slew/desync transient clears on re-read

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


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _post(url, path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode().strip()


def get_status(url):
    with urllib.request.urlopen(url + "/status", timeout=15) as r:
        return json.loads(r.read())


def get_metrics(url):
    with urllib.request.urlopen(url + "/metrics", timeout=15) as r:
        return r.read().decode()


def metric_value(metrics, name):
    for line in metrics.splitlines():
        if line.startswith(name + " "):
            try:
                return float(line.split(" ", 1)[1])
            except ValueError:
                return None
    return None


def scpi_ask(url, cmd):
    code, body = _post(url, "/scpi", {"ask": cmd})
    if code != 200 or not isinstance(body, dict):
        return None
    return body.get("reply")


def chan(status, ch):
    return (status.get("channels") or {}).get(str(ch))


def classify(c):
    if c is None:
        return "N/A"
    if c.get("trip_overvoltage") or c.get("trip_overcurrent"):
        return "TRIP"
    if c.get("constant_current"):
        return "CC"
    if c.get("constant_voltage"):
        return "CV"
    return "OFF"


def get_mode(url, ch):
    """Classify a channel, debouncing a transient trip (real trips latch)."""
    c = chan(get_status(url), ch)
    m = classify(c)
    if m == "TRIP":
        time.sleep(TRIP_DEBOUNCE)
        c = chan(get_status(url), ch)
        m = classify(c)
    return m, c


# --------------------------------------------------------------------------- #
# Applied commands - held and recorded
# --------------------------------------------------------------------------- #
def apply_ok(url, desc, payload):
    code, body = _post(url, "/control", payload)
    ok = check(desc, code == 200, body if code != 200 else "")
    return ok, body


def hold(url, writer, ch, phase, step, sv, si, dwell, sample):
    """Hold the current setpoint for `dwell`s, sampling status every `sample`s
    into the CSV. Returns the last mode; 'TRIP' means a latched trip."""
    end = time.time() + dwell
    last = "?"
    while True:
        mode, c = get_mode(url, ch)
        last = mode
        if c is not None:
            vo, io = c.get("output_voltage"), c.get("output_current")
            writer.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), phase, step,
                             ch, sv, si, vo, io,
                             round((vo or 0) * (io or 0), 4), mode])
        if mode == "TRIP":
            return "TRIP"
        if time.time() >= end:
            return last
        time.sleep(min(sample, max(0.0, end - time.time())))


# --------------------------------------------------------------------------- #
# Guard checks - safe: the exporter rejects these before touching the PSU
# --------------------------------------------------------------------------- #
def reject(url, desc, path, payload, want_code, want_substr=None):
    code, body = _post(url, path, payload)
    ok = code == want_code and (want_substr is None or
                                (isinstance(body, str) and want_substr in body))
    check(desc, ok, f"code={code} body={body!r}")


def run_guards(url, exp_maxv, exp_maxi):
    print("\n== guards (rejected before reaching the instrument - nothing energizes) ==")
    # absolute instrument range (independent of the operator ceiling)
    reject(url, "reject V above instrument range (61 V)", "/control",
           {"action": "set", "channel": "1", "voltage": 61}, 400)
    reject(url, "reject negative V (-1 V)", "/control",
           {"action": "set", "channel": "1", "voltage": -1}, 400)
    reject(url, "reject I above instrument range (11 A)", "/control",
           {"action": "set", "channel": "1", "current": 11}, 400)
    # PowerFlex envelope (checked before the ceiling): 5 A @ 60 V is over 180 W
    reject(url, "reject point outside PowerFlex envelope (5 A @ 60 V)", "/control",
           {"action": "set", "channel": "1", "voltage": 60, "current": 5}, 400, "envelope")
    # operator safety ceiling - only demonstrable when it is below hardware max
    if exp_maxv < 60:
        reject(url, f"reject V over operator ceiling ({exp_maxv + 0.5:g} V > {exp_maxv:g} V)",
               "/control", {"action": "set", "channel": "1", "voltage": exp_maxv + 0.5}, 400)
    else:
        print("  SKIP  operator voltage ceiling at hardware max - run exporter with "
              "--max-voltage to exercise it")
    if exp_maxi < 10:
        reject(url, f"reject I over operator ceiling ({exp_maxi + 0.1:g} A > {exp_maxi:g} A)",
               "/control", {"action": "set", "channel": "1", "current": exp_maxi + 0.1}, 400)
    else:
        print("  SKIP  operator current ceiling at hardware max")
    # OVP / OCP settable range
    reject(url, "reject OVP out of range (70 V)", "/control",
           {"action": "set", "channel": "1", "ovp": 70}, 400)
    reject(url, "reject OCP out of range (12 A)", "/control",
           {"action": "set", "channel": "1", "ocp": 12}, 400)
    # malformed control requests
    reject(url, "reject set with no field", "/control",
           {"action": "set", "channel": "1"}, 400)
    reject(url, "reject set on channel 'all'", "/control",
           {"action": "set", "channel": "all", "voltage": 1}, 400)
    reject(url, "reject unknown action", "/control", {"action": "kaboom"}, 400)
    # argument guards on the raw /scpi path (localhost-only)
    reject(url, "reject OP arg not 0/1 (OP1 2)", "/scpi", {"write": "OP1 2"}, 400)
    reject(url, "reject OPALL arg not 0/1 (OPALL 2)", "/scpi", {"write": "OPALL 2"}, 400)
    reject(url, "reject CONFIG not 0/2 (CONFIG 1)", "/scpi", {"write": "CONFIG 1"}, 400)
    reject(url, "reject RATIO out of 0-100 (RATIO 101)", "/scpi", {"write": "RATIO 101"}, 400)
    reject(url, "reject store out of 0-9 (SAV1 10)", "/scpi", {"write": "SAV1 10"}, 400)
    reject(url, "reject TRIPCONFIG not 0/1 (TRIPCONFIG 2)", "/scpi",
           {"write": "TRIPCONFIG 2"}, 400)
    reject(url, "reject raw V over instrument range (V1 61)", "/scpi", {"write": "V1 61"}, 400)
    print("  NOTE  localhost-only /control (remote 403) and /scpi auth are covered by "
          "test_guards.py; not exercisable from a single host")


# --------------------------------------------------------------------------- #
# Level helpers
# --------------------------------------------------------------------------- #
def voltage_profile(vmax, step):
    up = []
    v = step
    while v < vmax - 1e-9:
        up.append(round(v, 3))
        v += step
    up.append(round(vmax, 3))
    down = list(reversed(up[:-1])) + [0.0]
    return up + down


def current_profile(imax):
    steps = [0.25, 0.5, 0.75, 1.0]
    return [round(i, 3) for i in steps if i <= imax + 1e-9] or [round(imax, 3)]


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=config.EXPORTER_URL, help="exporter base URL")
    ap.add_argument("--channel", type=int, default=1, choices=[1, 2],
                    help="channel to sweep (energized)")
    ap.add_argument("--max-voltage", type=float, default=12.0, help="voltage cap for this run [V]")
    ap.add_argument("--max-current", type=float, default=1.0, help="current cap for this run [A]")
    ap.add_argument("--v-step", type=float, default=2.0, help="voltage staircase step [V]")
    ap.add_argument("--dwell", type=float, default=10.0, help="hold per applied setpoint [s]")
    ap.add_argument("--sample", type=float, default=1.0, help="status sample interval [s]")
    ap.add_argument("--local-pause", type=float, default=5.0, help="pause for the 'local' test [s]")
    ap.add_argument("--out", default="hw_verify.csv", help="CSV record of applied steps")
    ap.add_argument("--dry-run", action="store_true",
                    help="guards + readback only; do NOT energize anything")
    ap.add_argument("--allow-high-ceiling", action="store_true",
                    help="run even if the exporter ceiling is above the run cap (NOT recommended)")
    ap.add_argument("--restore-outputs", action="store_true",
                    help="re-enable outputs to their original on/off state at the end "
                         "(default: leave both off)")
    args = ap.parse_args()

    url = args.url
    ch = args.channel
    vmax, imax = args.max_voltage, args.max_current
    ovp_backstop = min(vmax + 1.0, 66.0)
    ocp_backstop = min(imax + 0.2, 11.0)

    # ---- preflight -------------------------------------------------------
    print(f"== preflight ({url}) ==")
    try:
        metrics = get_metrics(url)
        status = get_status(url)
    except OSError as exc:
        sys.exit(f"cannot reach exporter at {url} ({exc}); is cpx-exporter running "
                 "on this host?")
    if status.get("up") != 1:
        sys.exit("exporter is not connected to the PSU; aborting")
    check("instrument is a CPX200DP", "CPX200DP" in (status.get("idn") or ""), status.get("idn"))

    exp_maxv = metric_value(metrics, "cpx_max_voltage_volts")
    exp_maxi = metric_value(metrics, "cpx_max_current_amps")
    print(f"  exporter safety ceiling: {exp_maxv} V / {exp_maxi} A; "
          f"run cap: {vmax} V / {imax} A")
    if exp_maxv is None or exp_maxi is None:
        sys.exit("could not read the exporter's safety ceiling from /metrics")
    if (exp_maxv > vmax + 1e-9 or exp_maxi > imax + 1e-9) and not args.allow_high_ceiling:
        sys.exit(
            f"\nREFUSING TO ENERGIZE: the exporter's safety ceiling ({exp_maxv} V / "
            f"{exp_maxi} A) is above the run cap ({vmax} V / {imax} A). With the "
            "detector connected the exporter must itself refuse anything higher.\n"
            "Restart the exporter with:  --max-voltage 12 --max-current 1  (or set "
            "MAX_VOLTAGE=12 / MAX_CURRENT=1 in .env and restart), then re-run.\n"
            "Override with --allow-high-ceiling only if you understand the risk.")

    # capture originals so we can restore
    orig = {}
    for c in (1, 2):
        cs = chan(status, c) or {}
        ovp = scpi_ask(url, f"OVP{c}?")
        ocp = scpi_ask(url, f"OCP{c}?")
        orig[c] = {
            "v": cs.get("set_voltage"), "i": cs.get("set_current"),
            "op": cs.get("output_enabled"),
            "ovp": _num(ovp) if ovp else None,
            "ocp": _num(ocp) if ocp else None,
        }
        print(f"  original ch{c}: output={'ON' if orig[c]['op'] else 'off'} "
              f"V={orig[c]['v']} I={orig[c]['i']} OVP={orig[c]['ovp']} OCP={orig[c]['ocp']}")

    # ---- guards (always; safe) ------------------------------------------
    run_guards(url, exp_maxv, exp_maxi)

    if args.dry_run:
        print("\n-- dry run: skipping all energizing --")
        print(f"\n{passed} passed, {failed} failed")
        return 1 if failed else 0

    profile = voltage_profile(vmax, args.v_step)
    v_at = min(6.0, vmax)
    currents = current_profile(imax)
    n_holds = 2 + len(profile) + len(currents) + 1  # OPALL-on + ch-on + Vsweep + Isweep + off
    print(f"\n== energizing run on ch{ch}: ~{n_holds} holds x {args.dwell:g}s "
          f"(~{n_holds * args.dwell / 60:.1f} min) ==")
    print(f"  voltage profile: {profile}")
    print(f"  current-limit steps @ {v_at} V: {currents}")

    modified = True
    f = open(args.out, "w", newline="")
    writer = csv.writer(f)
    writer.writerow(["time", "phase", "step", "channel", "set_v", "set_i",
                     "out_v", "out_i", "power_w", "mode"])
    tripped = False
    try:
        # program both channels safe & OFF: start low, then OVP/OCP backstop
        apply_ok(url, "all outputs off", {"action": "off", "channel": "all"})
        apply_ok(url, "clear any latched trip", {"action": "triprst"})
        apply_ok(url, f"program both ch to 1 V / {min(0.5, imax):g} A, output off",
                 {"action": "set", "channel": "1", "voltage": 1.0, "current": min(0.5, imax)})
        apply_ok(url, "program ch2 likewise", {"action": "set", "channel": "2",
                 "voltage": 1.0, "current": min(0.5, imax)})
        for c in (1, 2):
            apply_ok(url, f"ch{c} OVP/OCP backstop ({ovp_backstop:g} V / {ocp_backstop:g} A)",
                     {"action": "set", "channel": str(c),
                      "ovp": ovp_backstop, "ocp": ocp_backstop})

        # OPALL on/off at a safe 1 V (verifies the all-channel switch)
        apply_ok(url, "OPALL on (both channels, 1 V)", {"action": "on", "channel": "all"})
        time.sleep(TURN_ON_SETTLE)
        hold(url, writer, ch, "opall", "both-on-1V", 1.0, min(0.5, imax), args.dwell, args.sample)
        apply_ok(url, "OPALL off", {"action": "off", "channel": "all"})

        # bring the swept channel up: program current limit, enable, ramp V
        apply_ok(url, f"ch{ch} current limit {imax:g} A (output off)",
                 {"action": "set", "channel": str(ch), "voltage": 0.0, "current": imax})
        apply_ok(url, f"ch{ch} output ON", {"action": "on", "channel": str(ch)})
        time.sleep(TURN_ON_SETTLE)

        for i, v in enumerate(profile):
            ok, _ = apply_ok(url, f"ch{ch} set {v:g} V", {"action": "set",
                             "channel": str(ch), "voltage": v})
            m = hold(url, writer, ch, "v-sweep", f"{v:g}V", v, imax, args.dwell, args.sample)
            print(f"    held {v:g} V -> mode {m}")
            if m == "TRIP":
                tripped = True
                check(f"ch{ch} did not trip at {v:g} V", False, "latched trip")
                break

        if not tripped:
            for cur in currents:
                apply_ok(url, f"ch{ch} at {v_at:g} V, current limit {cur:g} A",
                         {"action": "set", "channel": str(ch), "voltage": v_at, "current": cur})
                m = hold(url, writer, ch, "i-sweep", f"{cur:g}A", v_at, cur, args.dwell, args.sample)
                print(f"    held limit {cur:g} A @ {v_at:g} V -> mode {m}")
                if m == "TRIP":
                    tripped = True
                    break

        # triprst command (accepted)
        apply_ok(url, "triprst command accepted", {"action": "triprst"})

        # 'local' hands the panel back and pauses polling (a recording gap is expected)
        code, body = _post(url, "/control", {"action": "local", "pause": args.local_pause})
        check("local command accepted", code == 200 and isinstance(body, dict)
              and "paused_seconds" in body, body)
        if isinstance(body, dict):
            check("polling reports paused", metric_value(get_metrics(url), "cpx_polling_paused") == 1)

        # turn the swept channel off (re-enters remote mode) and record the collapse
        apply_ok(url, f"ch{ch} output OFF", {"action": "off", "channel": str(ch)})
        hold(url, writer, ch, "off", "output-off", 0.0, imax, args.dwell, args.sample)

    except KeyboardInterrupt:
        print("\n!! interrupted - restoring safe state")
    finally:
        f.close()
        restore(url, orig, args.restore_outputs)
        print(f"\nCSV written to {args.out}")
        print(f"{passed} passed, {failed} failed"
              + (" (a channel TRIPPED - see above)" if tripped else ""))

    return 1 if (failed or tripped) else 0


def restore(url, orig, restore_outputs):
    print("\n== restoring original state (outputs off, setpoints/OVP/OCP back) ==")
    _post(url, "/control", {"action": "off", "channel": "all"})
    _post(url, "/control", {"action": "triprst"})
    for c in (1, 2):
        o = orig[c]
        payload = {"action": "set", "channel": str(c)}
        if o["v"] is not None:
            payload["voltage"] = o["v"]
        if o["i"] is not None:
            payload["current"] = o["i"]
        if o["ovp"] is not None:
            payload["ovp"] = o["ovp"]
        if o["ocp"] is not None:
            payload["ocp"] = o["ocp"]
        if len(payload) > 2:
            code, body = _post(url, "/control", payload)
            check(f"ch{c} setpoints restored", code == 200, body)
    _post(url, "/control", {"action": "triprst"})  # clear any OVP-below-Vset latch
    if restore_outputs:
        for c in (1, 2):
            if orig[c]["op"]:
                _post(url, "/control", {"action": "on", "channel": str(c)})
        print("  outputs restored to their original on/off state")
    else:
        on = [c for c in (1, 2) if orig[c]["op"]]
        if on:
            print(f"  NOTE original state had ch{','.join(map(str, on))} ON; left OFF for "
                  "safety. Re-enable with:  ./psuctl on <ch>   (or re-run with --restore-outputs)")


if __name__ == "__main__":
    sys.exit(main())
