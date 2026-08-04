#!/usr/bin/env python3
"""Replay a real pscontrol values log as if the monitor were writing it now.

The end-to-end rehearsal for this stack: it produces a live-growing
<config>_values_<stamp>.log with REAL measured values, so ps_exporter, Prometheus,
the alert rules and the Grafana dashboard can all be exercised without power
supplies, without pccmsbril06, and without waiting hours for interesting data.

    # 1. replay 4 weeks of archive at 200x into a fresh log
    python3 replay-log.py ~/psdata/lab_cosmic_crocs/lab_cosmic_crocs_values_20260802_014650.log \\
        --out /tmp/psreplay --speed 200

    # 2. point the exporter at it (0.0.0.0 so the Prometheus container can reach it)
    cd ../../pscontrol/src && PS_EXPORTER_ADDR=0.0.0.0 python3 ps_exporter.py /tmp/psreplay

    # 3. PSLOG_EXPORTER_TARGET=host.docker.internal:9822 ./render.sh && ./on.sh

Rows are rewritten with the current time so Prometheus (which refuses samples far
from now) accepts them; every measured column is passed through byte-for-byte.
"""
import argparse
import os
import time


def rows(path):
    """Yield (header, list_of_value_rows) from a real values log."""
    header, data = None, []
    with open(path, errors="replace") as fh:
        for line in fh:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "DATETIME":
                header = line.rstrip("\n")
            else:
                data.append(parts)
    if header is None:
        raise SystemExit(f"{path}: no DATETIME header - is this a values log?")
    return header, data


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", help="a real *_values_*.log to replay")
    ap.add_argument("--out", default="/tmp/psreplay", help="directory to write the live log into")
    ap.add_argument("--speed", type=float, default=100.0,
                    help="replay rate multiplier; 1 = the original ~7s cadence")
    ap.add_argument("--rows", type=int, default=0, help="stop after N rows (0 = all)")
    ap.add_argument("--loop", action="store_true", help="restart at the top when the archive ends")
    args = ap.parse_args()

    header, data = rows(args.source)
    if args.rows:
        data = data[:args.rows]
    os.makedirs(args.out, exist_ok=True)
    config = os.path.basename(args.source).split("_values_")[0]
    path = os.path.join(args.out, f"{config}_values_{time.strftime('%Y%m%d_%H%M%S')}.log")

    # The original cadence, so --speed is relative to how the monitor really behaves.
    delay = 7.0 / args.speed
    print(f"replaying {len(data)} rows from {os.path.basename(args.source)}\n"
          f"  -> {path}\n"
          f"  {args.speed:g}x ({delay:.3f}s/row){' looping' if args.loop else ''}", flush=True)

    written = 0
    with open(path, "a", buffering=1) as fh:      # line-buffered, like psmap's flush=True
        fh.write(header + "\n")
        while True:
            for parts in data:
                # stamp with now; keep every measured column exactly as recorded
                fh.write(f"{time.strftime('%Y-%m-%d_%H:%M:%S'):<20}"
                         + "".join(f"{v:<20}" for v in parts[1:]).rstrip() + "\n")
                written += 1
                if written % 500 == 0:
                    print(f"  {written} rows", flush=True)
                time.sleep(delay)
            if not args.loop:
                break
    print(f"done, {written} rows", flush=True)


if __name__ == "__main__":
    main()
