"""OOB collaborator listener -- the out-of-band confirmation channel.

A blind bug (SSRF, exfil, server-side request from an HTTP task / webhook / event
queue, deferred RCE) produces no in-band signal. To CONFIRM rather than merely
hypothesize it, the agent plants a unique canary URL (``sc.oob()`` -> this server)
into a feature, triggers the flow, and the harness checks whether the target's
server reached the canary. An inbound hit is proof.

This is one stdlib process with two faces:
  - PUBLIC (via the cloudflared tunnel): any request to ``/c/<token>...`` is
    recorded (method, full path, source IP, headers, body, time). That URL is what
    gets planted in the target.
  - LOCAL (queried by the oob_check worker on 127.0.0.1): ``/_oob/hits?token=T``
    returns recorded hits; ``/_oob/url`` returns the public base; ``/_oob/health``.

In-memory store (process lifetime == assessment lifetime); no deps so it runs on
the host next to the other workers.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("SC_OOB_PORT", "8099"))
PUBLIC_BASE = os.environ.get("SC_OOB_PUBLIC_BASE", "")  # set once the tunnel URL is known
MAX_HITS = 2000
MAX_BODY = 8192

_lock = threading.Lock()
_hits: dict[str, list] = defaultdict(list)   # token -> [hit, ...]
_all: list = []                               # chronological, capped


def _record(method: str, path: str, headers, client_ip: str, body: bytes):
    # token = first path segment after /c/ ; everything else is extra path the
    # target may have appended (e.g. /c/<token>/latest/meta-data/).
    token = ""
    parts = [p for p in path.split("?")[0].split("/") if p]
    if len(parts) >= 2 and parts[0] == "c":
        token = parts[1]
    hit = {
        "ts": int(time.time()),
        "token": token,
        "method": method,
        "path": path,
        "client_ip": client_ip,
        "headers": {k: v for k, v in headers.items()},
        "body": (body or b"")[:MAX_BODY].decode("utf-8", "replace"),
    }
    with _lock:
        if token:
            _hits[token].append(hit)
        _all.append(hit)
        if len(_all) > MAX_HITS:
            del _all[: len(_all) - MAX_HITS]
    return hit


class Handler(BaseHTTPRequestHandler):
    server_version = "sc-oob/0.1"

    def log_message(self, *a):  # quiet
        pass

    def _json(self, code: int, obj):
        payload = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _api(self):
        path = self.path
        if path.startswith("/_oob/health"):
            return self._json(200, {"ok": True, "public_base": PUBLIC_BASE,
                                    "tokens": len(_hits)})
        if path.startswith("/_oob/url"):
            return self._json(200, {"public_base": PUBLIC_BASE})
        if path.startswith("/_oob/hits"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(path).query)
            token = (q.get("token") or [""])[0]
            with _lock:
                hits = list(_hits.get(token, [])) if token else list(_all)
            return self._json(200, {"token": token, "count": len(hits), "hits": hits})
        return self._json(404, {"error": "unknown _oob endpoint"})

    def _handle(self):
        if self.path.startswith("/_oob/"):
            return self._api()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, MAX_BODY)) if length else b""
        _record(self.command, self.path, self.headers,
                 self.client_address[0] if self.client_address else "", body)
        # Bland 200 so the target's fetch "succeeds" and the flow proceeds.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_HEAD = _handle
    do_PATCH = _handle


def main():
    global PUBLIC_BASE
    if len(sys.argv) > 1:
        PUBLIC_BASE = sys.argv[1].rstrip("/")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"oob listener on 127.0.0.1:{PORT} public_base={PUBLIC_BASE or '(pending)'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
