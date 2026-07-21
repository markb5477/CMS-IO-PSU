#!/usr/bin/env python3
"""Generate a bertContinuous.csv exactly like BERTCSVMetricsSink writes, for
exercising the exporter without the DAQ.

    python3 tests/fake_bert_csv.py /tmp/bert/Results/run1/bertContinuous.csv           # write 30 rows
    python3 tests/fake_bert_csv.py /tmp/bert/Results/run1/bertContinuous.csv --live    # keep appending

The row shape matches Utils/BERTCSVMetricsSink.cc: it cycles boards/hybrids/lines
and writes the header only when the file is new/empty. Includes the fecUplink/
fecDownlink columns added at Run_43, with a slowly climbing uplink count so the
FEC panels have something to draw.
"""
import argparse
import os
import time
from datetime import datetime, timezone

HEADER = "timestamp,board,hybrid,line,testedBits,errorCount,fecUplink,fecDownlink"
BOARDS = (0,)
HYBRIDS = (0, 1)
LINES = range(7)              # PS module: 7 lines
TESTED_PER_SAMPLE = 10_000_000_000


def row(board, hybrid, line, errors, fec_up, fec_down):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (f"{ts},{board},{hybrid},{line},{TESTED_PER_SAMPLE},{errors},"
            f"{fec_up},{fec_down}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--live", action="store_true", help="append forever, one row/sec")
    ap.add_argument("--rows", type=int, default=30, help="rows to write in one-shot mode")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.path)), exist_ok=True)
    write_header = not os.path.exists(args.path) or os.path.getsize(args.path) == 0

    combos = [(b, h, ln) for b in BOARDS for h in HYBRIDS for ln in LINES]
    with open(args.path, "a", buffering=1) as fh:   # line-buffered, like the C++ std::endl flush
        if write_header:
            fh.write(HEADER + "\n")
        i = 0
        while True:
            b, h, ln = combos[i % len(combos)]
            # a couple of "links" degrade so BER varies across series
            errors = 12 if (h, ln) == (1, 3) else (0 if ln % 2 else 3)
            # FEC is per optical group, so every hybrid on a board reports the
            # same count - the exporter maxes rather than sums for this reason.
            fec_up = i // len(combos) if h == 1 else 0
            fh.write(row(b, h, ln, errors, fec_up, 0) + "\n")
            i += 1
            if not args.live and i >= args.rows:
                break
            if args.live:
                time.sleep(1)
    print(f"wrote {i} rows to {args.path}")


if __name__ == "__main__":
    main()
