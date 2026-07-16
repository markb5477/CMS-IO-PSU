"""Shared config for the PSU-server tools (exporter, psuctl, experiments).

The values live in one place - the .env file next to this module (override the
path with $PSU_ENV). config.py only reads them; it keeps no copy of its own, so
there is nothing to fall out of sync. Real environment variables win, so
systemd Environment= or a one-off `HTTP_PORT=... ./psuctl ...` still override.
"""

import os

_ENV_PATH = os.environ.get(
    "PSU_ENV", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


def _req(key):
    val = os.environ.get(key)
    if val is None:
        raise SystemExit(f"config: {key} is not set; add it to {_ENV_PATH} "
                         "(copy .env.example to .env)")
    return val


PSU_HOST = _req("PSU_HOST")
PSU_PORT = int(_req("PSU_PORT"))
LISTEN = _req("LISTEN")                    # exporter bind address
HTTP_HOST = _req("HTTP_HOST")              # where clients reach it
HTTP_PORT = int(_req("HTTP_PORT"))
POLL_INTERVAL = float(_req("POLL_INTERVAL"))
MAX_VOLTAGE = float(_req("MAX_VOLTAGE"))
MAX_CURRENT = float(_req("MAX_CURRENT"))
EXPORTER_URL = f"http://{HTTP_HOST}:{HTTP_PORT}"
