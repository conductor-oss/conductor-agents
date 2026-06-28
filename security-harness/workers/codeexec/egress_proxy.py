"""Allow-listing forward proxy -- the network jail for the code_exec sandbox.

The sandbox container is attached ONLY to an internal Docker network (no NAT, no
route to the internet), and is pointed at this proxy via HTTP(S)_PROXY. The proxy
straddles the internal network and an egress network and forwards a request ONLY
if its host matches the allow-list (the target host + the OOB collaborator domain).
A raw socket from agent code therefore has nowhere to go except the proxy, and the
proxy refuses anything off the allow-list -- so even deliberately-written exfil code
cannot leak the injected tokens to an arbitrary host. This is defense-in-depth on
top of the in-sandbox `sc` scope check.

Allow-list arrives via SC_ALLOW (comma-separated host suffixes, e.g.
"your-conductor.example.com,trycloudflare.com"). Pure stdlib so it runs in the same
sc-codeexec image with no extra deps. Handles both plain HTTP (Host header) and
HTTPS (CONNECT host:port tunnel).
"""

from __future__ import annotations

import os
import select
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("SC_PROXY_PORT", "8888"))
ALLOW = [h.strip().lower() for h in (os.environ.get("SC_ALLOW") or "").split(",") if h.strip()]


def _allowed(host: str) -> bool:
    host = (host or "").lower().split(":")[0]
    if not host:
        return False
    return any(host == a or host.endswith("." + a) for a in ALLOW)


def _pipe(a: socket.socket, b: socket.socket):
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, 30)
            if x or not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _deny(self):
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_CONNECT(self):  # HTTPS tunnel
        host, _, port = self.path.partition(":")
        if not _allowed(host):
            return self._deny()
        try:
            upstream = socket.create_connection((host, int(port or 443)), timeout=15)
        except OSError:
            self.send_response(502); self.send_header("Content-Length", "0"); self.end_headers()
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        _pipe(self.connection, upstream)

    def _http(self):  # plain HTTP forward
        host = urlparse(self.path).hostname or (self.headers.get("Host") or "").split(":")[0]
        if not _allowed(host):
            return self._deny()
        try:
            import urllib.request
            body = None
            cl = int(self.headers.get("Content-Length") or 0)
            if cl:
                body = self.rfile.read(cl)
            req = urllib.request.Request(self.path, data=body, method=self.command)
            for k, v in self.headers.items():
                if k.lower() not in ("proxy-connection", "connection"):
                    req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception:
            self.send_response(502); self.send_header("Content-Length", "0"); self.end_headers()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _http


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Proxy)
    print(f"egress proxy on :{PORT} allow={ALLOW}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
