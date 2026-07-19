#!/usr/bin/env python3
"""Serve the FakeCPX (tests/fakepsu.py) SCPI over TCP, so the *real* exporter
can connect to it for local end-to-end validation - no instrument, no lab PC.

    python3 tests/fake_psu_server.py --port 9221     # then point exporter at it

Query lines (ending in '?') get one reply line; everything else is applied
silently, exactly like the line-based CPX. One shared instrument state, so the
exporter (the single socket owner) sees a consistent PSU.
"""

import argparse
import socketserver

from fakepsu import FakeCPX

INSTRUMENT = FakeCPX()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            line = raw.decode("ascii", "replace").strip()
            if not line:
                continue
            if line.endswith("?"):
                try:
                    reply = INSTRUMENT._answer(line)
                except ValueError as exc:
                    reply = f"error {exc}"
                self.wfile.write((reply + "\n").encode("ascii"))
            else:
                INSTRUMENT._apply(line)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9221)
    args = ap.parse_args()
    with Server((args.host, args.port), Handler) as srv:
        print(f"fake CPX200DP listening on {args.host}:{args.port}", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
