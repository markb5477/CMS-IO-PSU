#!/usr/bin/env python3
"""Verdict on an hw_verify CSV: did each hold settle to its setpoint, did the
rail collapse when the output went off, and were there any trips?

It judges the SETTLED tail of each hold (the last third), so the slew transient
right after a setpoint change doesn't count against it; it keeps ascending and
descending visits to the same level as separate holds (grouping by contiguous
runs, not by unique label); and it treats the open-terminal CV-status-bit
flicker (a stray 'OFF' sample mid-hold) as a cosmetic note, not a failure -
matching how readonly.py / the experiments describe an unloaded CPX.

    python3 tests/check_run.py                     # newest hw_verify_*.csv in cwd
    python3 tests/check_run.py hw_verify_2026-07-17_0921.csv
"""
import csv
import glob
import os
import statistics as st
import sys

TOL = 0.25          # settled rail vs setpoint (V)
OFF_MAX = 0.5       # a collapsed rail should end below this (V)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        cands = sorted(glob.glob("hw_verify_*.csv"), key=os.path.getmtime, reverse=True)
        if not cands:
            sys.exit("no CSV given and no hw_verify_*.csv in the current directory")
        path = cands[0]

    rows = list(csv.DictReader(open(path)))
    if not rows:
        sys.exit(f"{path}: no rows")

    # contiguous runs of (phase, step) -> one hold each (so up/down visits split)
    holds = []
    for r in rows:
        key = (r["phase"], r["step"])
        if not holds or holds[-1][0] != key:
            holds.append((key, []))
        holds[-1][1].append(r)

    ok, notes = True, []
    print(f"{path}: {len(rows)} samples, {len(holds)} holds\n")
    print(f"{'phase':9}{'step':8}{'n':>3}  {'set_v':>6}  {'settled_v':>9}  {'range':>13}  modes")
    for (phase, step), g in holds:
        outv = [num(r["out_v"]) for r in g if num(r["out_v"]) is not None]
        setv, modes = num(g[0]["set_v"]), [r["mode"] for r in g]
        mset = ",".join(sorted(set(modes)))
        tail = outv[max(1, 2 * len(outv) // 3):] or outv     # last third = settled
        settled = st.mean(tail) if tail else None
        rng = f"{min(outv):.2f}..{max(outv):.2f}" if outv else "-"
        sv = "-" if setv is None else f"{setv:.2f}"
        sv_out = "-" if settled is None else f"{settled:.2f}"
        print(f"{phase:9}{step:8}{len(g):>3}  {sv:>6}  {sv_out:>9}  {rng:>13}  {mset}")

        energized = phase in ("v-sweep", "i-sweep", "opall")
        if "TRIP" in modes:
            ok = False
            print(f"   !! TRIP during {phase}/{step}")
        if energized and setv and settled is not None and abs(settled - setv) > TOL:
            ok = False
            print(f"   !! settled rail {settled:.2f} V off setpoint {setv:.2f} V")
        if phase == "off" and settled is not None and settled > OFF_MAX:
            ok = False
            print(f"   !! rail not collapsed by end of off-hold: {settled:.2f} V")
        if energized and "OFF" in modes:
            notes.append(f"{phase}/{step}: CV bit flickered to OFF mid-hold "
                         "(open-terminal cosmetic; V correct)")

    print("\nVERDICT:", "ALL GOOD - every hold settled to setpoint, off-rail collapsed, "
          "no trips" if ok else "REAL ISSUES ABOVE")
    if notes:
        print("\nnotes (expected with open terminals, not failures):")
        for n in dict.fromkeys(notes):
            print("  -", n)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
