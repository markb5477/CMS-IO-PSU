"""pymeasure Adapter that reaches the PSU through the exporter's /scpi
endpoint, so the exporter stays the single socket owner and setpoints are
still safety-checked. A '?' command is asked and its reply stashed for the
next _read(); anything else is a plain write."""

import json
import os
import sys
import urllib.error
import urllib.request

from pymeasure.adapters import Adapter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


class GatewayAdapter(Adapter):
    def __init__(self, base_url=None, timeout=15, **kwargs):
        super().__init__(**kwargs)
        self.url = (base_url or config.EXPORTER_URL).rstrip("/") + "/scpi"
        self.timeout = timeout
        self._pending_reply = None

    def _post(self, payload):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.url, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # 400 = rejected setpoint; surface the exporter's message
            raise ConnectionError(exc.read().decode().strip()) from None

    def _write(self, command, **kwargs):
        if command.strip().endswith("?"):
            self._pending_reply = self._post({"ask": command})["reply"]
        else:
            self._post({"write": command})
            self._pending_reply = None

    def _read(self, **kwargs):
        if self._pending_reply is None:
            raise ConnectionError("no reply pending; last command was not a query")
        reply, self._pending_reply = self._pending_reply, None
        return reply

    def __repr__(self):
        return f"<GatewayAdapter(url={self.url!r})>"
