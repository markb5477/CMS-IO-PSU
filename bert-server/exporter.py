#!/usr/bin/env python3
"""Prometheus exporter for the mm_acf continuous BER test log.

The DAQ's OTBitErrorRateTestContinuous appends CSV rows to bertContinuous.csv:

    timestamp,board,hybrid,line,testedBits,errorCount
    2026-07-20T08:31:00Z,0,0,3,10000000000,12

Prometheus can't read a file, only HTTP. This exporter is the adapter: it follows
the newest bertContinuous.csv (or a pinned path), keeps the *latest* row per
(board,hybrid,line), and renders that snapshot on GET /metrics. It stores no
history of its own - Prometheus builds the time series by scraping repeatedly.

It reads only the newly-appended bytes each poll (a retained byte offset), so it
stays cheap even after the file has grown for weeks, and it resets when a new run
rotates the file (path change or truncation).

    GET  /metrics   Prometheus format
    GET  /status    current snapshot as JSON

Stdlib only.
"""

import argparse
import glob
import json
import math
import os
import socketserver
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

# The DAQ (mm_acf D19cBERTinterface) writes hardware read-failure SENTINELS straight
# into the CSV rather than raising, so a row can carry a bogus value that must not be
# mistaken for a real measurement:
#   - getBitErrorCounters() returns 0xFFFFFFFF when the FPGA error counter never gave
#     3 stable reads -> lands in errorCount. (NB: also indistinguishable from a
#     genuinely saturated 32-bit counter; both mean "don't trust this sample".)
#   - readNumberOfTestedBit() builds testedBits from getFrameCounters(), which returns
#     0xFFFFFFFFFFFFFFFF on the same failure; after (<<32)|... and a float multiply it
#     lands astronomically large. Real per-sample counts are far below the cap below.
ERROR_COUNT_SENTINEL = 0xFFFFFFFF          # 4294967295
MAX_PLAUSIBLE_TESTED_BITS = 1e15           # heuristic ceiling; see BERT_MAX_TESTED_BITS


class HTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # skip the reverse-DNS lookup that stalls startup by ~5s
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def parse_timestamp(text):
    """'2026-07-20T08:31:00Z' -> unix seconds (UTC). Raises ValueError on junk."""
    dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.timestamp()


class CsvSource:
    """Follows one bertContinuous.csv and holds the latest sample per link.

    Thread-safe: poll() (background) and get_snapshot()/render (HTTP) coordinate
    through state_lock.
    """

    def __init__(self, explicit_path="", results_root=".", glob_pattern="**/bertContinuous.csv",
                 error_sentinel=ERROR_COUNT_SENTINEL, max_tested_bits=MAX_PLAUSIBLE_TESTED_BITS):
        self.explicit_path = explicit_path
        self.results_root = results_root
        self.glob_pattern = glob_pattern
        # error_sentinel < 0 (or None) disables the errorCount-sentinel check.
        self.error_sentinel = None if (error_sentinel is None or error_sentinel < 0) else error_sentinel
        # max_tested_bits <= 0 disables the implausible-testedBits check.
        self.max_tested_bits = max_tested_bits if max_tested_bits and max_tested_bits > 0 else float("inf")

        self.state_lock = threading.Lock()
        # per (board, hybrid, line) -> {"error_count", "tested_bits", "ber", "ts", "valid"}
        # Values are NaN when the sample was a sentinel / undefined (see _parse_line).
        self.series = {}
        self.path = None          # file currently being followed
        self.inode = None
        self.offset = 0           # bytes consumed so far in self.path
        self.rows = 0             # data rows parsed from the current file
        self.file_mtime = 0.0
        self.file_size = 0
        self.up = 0               # 1 if the last poll found and read a file
        self.file_present = 0
        self.read_errors = 0      # lifetime counter: stat/open/read failures
        self.parse_errors = 0     # lifetime counter: unparseable rows skipped
        self.invalid_samples = 0  # lifetime counter: rows parsed but value was a sentinel/undefined
        self.render_errors = 0    # lifetime counter: exceptions while rendering /metrics
        self.last_error = ""

    def resolve_path(self):
        """Return the file to follow, or None. Explicit path wins; otherwise the
        newest glob match by mtime."""
        if self.explicit_path:
            return self.explicit_path if os.path.isfile(self.explicit_path) else None
        pattern = os.path.join(self.results_root, self.glob_pattern)
        matches = [p for p in glob.glob(pattern, recursive=True) if os.path.isfile(p)]
        if not matches:
            return None
        return max(matches, key=os.path.getmtime)

    def _parse_line(self, line):
        # Never let one malformed row abort a poll: any unexpected parse issue is
        # counted and skipped, not raised.
        try:
            parts = line.split(",")
            if len(parts) < 6:
                if not (parts and parts[0] == "timestamp"):
                    self.parse_errors += 1
                return
            if parts[0] == "timestamp" or parts[1] == "board":   # header row
                return
            ts = parse_timestamp(parts[0])
            board = int(parts[1])
            hybrid = int(parts[2])
            line_no = int(parts[3])
            tested = int(parts[4])
            errors = int(parts[5])
            if min(board, hybrid, line_no, tested, errors) < 0:
                self.parse_errors += 1
                return
        except (ValueError, IndexError):
            self.parse_errors += 1
            return

        # Classify the sample. A row can be well-formed yet carry a hardware
        # sentinel; treat those as "no measurement" (NaN) rather than a real value,
        # so Grafana shows a gap instead of a spurious 4-billion-error spike.
        nan = float("nan")
        error_bad = (self.error_sentinel is not None and errors == self.error_sentinel)
        tested_bad = (tested == 0 or tested > self.max_tested_bits)
        valid = not (error_bad or tested_bad)
        if not valid:
            self.invalid_samples += 1

        self.series[(board, hybrid, line_no)] = {
            "error_count": nan if error_bad else errors,
            "tested_bits": nan if tested_bad else tested,
            "ber": (errors / tested) if valid else nan,
            "ts": ts,          # the timestamp is still trustworthy: the DAQ did write a row
            "valid": valid,
        }
        self.rows += 1

    def poll(self):
        """Read newly-appended rows; reset on file rotation/truncation."""
        try:
            path = self.resolve_path()
            if path is None:
                with self.state_lock:
                    self.up = 0
                    self.file_present = 0
                return
            st = os.stat(path)
            with self.state_lock:
                rotated = (path != self.path or st.st_ino != self.inode
                           or st.st_size < self.offset)
                if rotated:
                    self.path = path
                    self.inode = st.st_ino
                    self.offset = 0
                    self.series = {}
                    self.rows = 0
                offset = self.offset

            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read()

            # Only consume up to the last complete line; a trailing partial line
            # (writer mid-append) is left for next time.
            nl = data.rfind(b"\n")
            consumed = data[:nl + 1] if nl != -1 else b""

            with self.state_lock:
                for raw in consumed.decode("utf-8", "replace").splitlines():
                    raw = raw.strip()
                    if raw:
                        self._parse_line(raw)
                self.offset += len(consumed)
                self.file_mtime = st.st_mtime
                self.file_size = st.st_size
                self.file_present = 1
                self.up = 1
        except Exception as exc:
            # Any failure (stat race, glob, decode, unexpected) -> report down and
            # keep the last-known series; never crash the poll thread.
            with self.state_lock:
                self.read_errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.up = 0

    def note_render_error(self, exc):
        with self.state_lock:
            self.render_errors += 1
            self.last_error = f"render: {type(exc).__name__}: {exc}"

    def get_snapshot(self):
        with self.state_lock:
            series = {k: dict(v) for k, v in self.series.items()}
            last_ts = max((v["ts"] for v in series.values()), default=0.0)
            return {
                "up": self.up,
                "file_present": self.file_present,
                "path": self.path,
                "rows": self.rows,
                "active_series": len(series),
                "valid_series": sum(1 for v in series.values() if v.get("valid")),
                "last_sample_timestamp": last_ts,
                "file_mtime": self.file_mtime,
                "file_size": self.file_size,
                "read_errors": self.read_errors,
                "parse_errors": self.parse_errors,
                "invalid_samples": self.invalid_samples,
                "render_errors": self.render_errors,
                "last_error": self.last_error,
                "series": series,
            }


class Reader:
    """Background thread that polls the CsvSource on a fixed cadence."""

    def __init__(self, source, interval):
        self.source = source
        self.interval = interval

    def start(self):
        self.source.poll()   # one poll before serving, so the first scrape has data
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            time.sleep(self.interval)
            self.source.poll()


def _escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _fmt(value):
    """Prometheus text value: NaN floats become the literal 'NaN' (a valid Prometheus
    value that renders as a gap in Grafana), ints/normal floats print plainly."""
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "+Inf" if value > 0 else "-Inf"
        return repr(value)
    return str(value)


def _json_scalar(value):
    # strict JSON has no NaN/Inf; map them to null so /status parses everywhere
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def status_json(snap):
    """A JSON-serialisable view of a snapshot: the series dict is keyed by a
    (board,hybrid,line) tuple, which json.dumps can't encode, so flatten it to a
    sorted list of per-link records; NaN/Inf become null."""
    out = {k: _json_scalar(v) for k, v in snap.items() if k != "series"}
    out["series"] = [
        {"board": b, "hybrid": h, "line": ln, **{k: _json_scalar(v) for k, v in rec.items()}}
        for (b, h, ln), rec in sorted(snap["series"].items())
    ]
    return out


def render_metrics(source):
    snap = source.get_snapshot()
    out = []

    def metric(name, help_text, mtype, samples):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {mtype}")
        for labels, value in samples:
            out.append(f"{name}{labels} {value}")

    metric("bert_up", "1 if the last poll found and read the BER log", "gauge",
           [("", snap["up"])])
    metric("bert_file_present", "1 if a bertContinuous.csv was found to follow", "gauge",
           [("", snap["file_present"])])
    metric("bert_read_errors_total", "File stat/open/read failures since start", "counter",
           [("", snap["read_errors"])])
    metric("bert_parse_errors_total", "CSV rows skipped as unparseable since start", "counter",
           [("", snap["parse_errors"])])
    metric("bert_invalid_samples_total",
           "Rows parsed but whose value was a read-failure sentinel or undefined "
           "(BER exported as NaN)", "counter", [("", snap["invalid_samples"])])
    metric("bert_render_errors_total", "Exceptions while rendering /metrics since start",
           "counter", [("", snap["render_errors"])])
    metric("bert_file_rows", "Data rows parsed from the current log file", "gauge",
           [("", snap["rows"])])
    metric("bert_active_series", "Distinct (board,hybrid,line) links seen in this run", "gauge",
           [("", snap["active_series"])])
    metric("bert_valid_series", "Links whose latest sample was a valid measurement (not a sentinel)",
           "gauge", [("", snap["valid_series"])])
    metric("bert_last_sample_timestamp_seconds",
           "Timestamp of the newest sample in the log (unix seconds); "
           "time() - this = data staleness", "gauge",
           [("", f"{snap['last_sample_timestamp']:.0f}")])
    metric("bert_file_mtime_seconds", "Last-modified time of the log file (unix seconds)",
           "gauge", [("", f"{snap['file_mtime']:.0f}")])
    metric("bert_file_size_bytes", "Size of the log file", "gauge",
           [("", snap["file_size"])])
    if snap["path"]:
        metric("bert_info", "The log file currently being followed", "gauge",
               [(f'{{path="{_escape(snap["path"])}"}}', 1)])

    series = sorted(snap["series"].items())
    if series:
        def per_link(name, help_text, key, fmt=_fmt):
            metric(name, help_text, "gauge",
                   [(f'{{board="{b}",hybrid="{h}",line="{ln}"}}', fmt(v[key]))
                    for (b, h, ln), v in series])

        per_link("bert_bit_error_rate",
                 "errorCount / testedBits of the latest sample (NaN if the sample was invalid)", "ber")
        per_link("bert_error_count", "errorCount of the latest sample (NaN if the counter read failed)",
                 "error_count")
        per_link("bert_tested_bits", "testedBits of the latest sample (NaN if implausible/failed)",
                 "tested_bits")
        per_link("bert_sample_valid",
                 "1 if this link's latest sample was a valid measurement, 0 if a sentinel/undefined",
                 "valid", lambda v: "1" if v else "0")
        per_link("bert_sample_timestamp_seconds",
                 "Timestamp of this link's latest sample (unix seconds)",
                 "ts", lambda x: f"{x:.0f}")
    return "\n".join(out) + "\n"


def make_handler(source):
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
                try:
                    body = render_metrics(source)
                except Exception as exc:
                    # A scrape must always get valid, parseable Prometheus text.
                    source.note_render_error(exc)
                    body = ("# HELP bert_up 1 if the last poll found and read the BER log\n"
                            "# TYPE bert_up gauge\nbert_up 0\n"
                            "# HELP bert_render_errors_total Exceptions while rendering /metrics\n"
                            "# TYPE bert_render_errors_total counter\n"
                            f"bert_render_errors_total {source.render_errors}\n")
                self._reply(200, body, "text/plain; version=0.0.4; charset=utf-8")
            elif self.path == "/status":
                try:
                    body = json.dumps(status_json(source.get_snapshot()), indent=2)
                except Exception as exc:
                    body = json.dumps({"up": 0, "error": f"{type(exc).__name__}: {exc}"})
                self._reply(200, body, "application/json")
            elif self.path == "/":
                self._reply(200, "bert-exporter: GET /metrics, GET /status\n")
            else:
                self._reply(404, "not found\n")

        def log_message(self, fmt, *args):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default=config.LISTEN, help="HTTP bind address")
    ap.add_argument("--port", type=int, default=config.HTTP_PORT, help="HTTP port for /metrics")
    ap.add_argument("--interval", type=float, default=config.POLL_INTERVAL,
                    help="how often to re-read the CSV [s]")
    ap.add_argument("--csv", default=config.CSV,
                    help="explicit CSV path to follow (wins over --results-root/--glob)")
    ap.add_argument("--results-root", default=config.RESULTS_ROOT,
                    help="root directory to search for the CSV")
    ap.add_argument("--glob", default=config.GLOB,
                    help="glob (under --results-root) for the CSV; newest match is followed")
    ap.add_argument("--error-sentinel", type=int, default=config.ERROR_SENTINEL,
                    help="errorCount value meaning 'FPGA read failed' -> NaN (negative disables)")
    ap.add_argument("--max-tested-bits", type=float, default=config.MAX_TESTED_BITS,
                    help="testedBits above this is treated as a sentinel -> NaN (<=0 disables)")
    args = ap.parse_args()

    source = CsvSource(args.csv, args.results_root, args.glob,
                       error_sentinel=args.error_sentinel, max_tested_bits=args.max_tested_bits)
    Reader(source, args.interval).start()

    server = HTTPServer((args.listen, args.port), make_handler(source))
    target = args.csv if args.csv else f"{os.path.join(args.results_root, args.glob)} (newest)"
    print(f"listening on {args.listen}:{args.port}, following {target} "
          f"every {args.interval}s", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
