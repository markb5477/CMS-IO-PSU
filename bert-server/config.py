"""Shared config for the BERT status exporter.

Mirrors psu-server/config.py: values live in one .env file next to this module
(override the path with $BERT_ENV). Real environment variables win, so a systemd
Environment= or a one-off `BERT_HTTP_PORT=... ./exporter.py` still overrides.

Unlike the PSU exporter every value here has a sane default, because the whole
point of this exporter is that it can start *before* we know the on-site path
and be pointed at the right CSV later (edit .env, restart).
"""

import os

_ENV_PATH = os.environ.get(
    "BERT_ENV", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


def _get(key, default):
    val = os.environ.get(key)
    return default if val is None or val == "" else val


# --- HTTP endpoint Prometheus scrapes -------------------------------------
LISTEN = _get("BERT_LISTEN", "127.0.0.1")          # exporter bind address
HTTP_HOST = _get("BERT_HTTP_HOST", "127.0.0.1")    # where clients reach it
HTTP_PORT = int(_get("BERT_HTTP_PORT", "9821"))
POLL_INTERVAL = float(_get("BERT_POLL_INTERVAL", "2"))  # how often to re-read the CSV [s]

# --- Which CSV to follow ---------------------------------------------------
# Two modes, checked in this order:
#   1. BERT_CSV set          -> follow exactly that file (pin it once on-site).
#   2. otherwise             -> glob BERT_GLOB under BERT_RESULTS_ROOT and follow
#                               the most-recently-modified match. This tracks the
#                               run-specific Results/OT_ModuleTest_<id>_Run<n>/
#                               directory that mm_acf creates for each run.
CSV = _get("BERT_CSV", "")                          # explicit path (wins if set)
RESULTS_ROOT = _get("BERT_RESULTS_ROOT", ".")       # root to search for the CSV
GLOB = _get("BERT_GLOB", "**/bertContinuous.csv")   # basename is stable across runs

# --- Robustness: treat hardware read-failure sentinels as NaN (not real data) ---
# The DAQ writes 0xFFFFFFFF into errorCount when the FPGA counter read never
# stabilised, and an astronomically large testedBits when the frame-counter read
# failed. Both are exported as NaN (a gap in Grafana) rather than a bogus spike.
ERROR_SENTINEL = int(_get("BERT_ERROR_SENTINEL", "4294967295"))   # negative disables
MAX_TESTED_BITS = float(_get("BERT_MAX_TESTED_BITS", "1e15"))     # <=0 disables

EXPORTER_URL = f"http://{HTTP_HOST}:{HTTP_PORT}"
