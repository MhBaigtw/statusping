"""Tiny local HTTP servers on 127.0.0.1 for the worker-process test and scripts/e2e.py.

FakeTarget answers every GET/HEAD with `.status` (flip it to simulate an outage).
FakeWebhook records every POSTed JSON body in `.received`.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Server:
    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.owner = self
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path="/"):
        return f"http://127.0.0.1:{self.port}{path}"


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _TargetHandler(_Quiet):
    def _answer(self, with_body):
        owner = self.server.owner
        owner.hits += 1
        self.send_response(owner.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        if with_body:
            self.wfile.write(b"ok")

    def do_GET(self):  # noqa: N802
        self._answer(True)

    def do_HEAD(self):  # noqa: N802
        self._answer(False)


class FakeTarget(_Server):
    def __init__(self, status=200):
        super().__init__(_TargetHandler)
        self.status = status
        self.hits = 0


class _WebhookHandler(_Quiet):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        owner = self.server.owner
        with owner.lock:
            owner.received.append(body)
        self.send_response(204)
        self.end_headers()


class SlowServer:
    """A raw-socket server on 127.0.0.1 that misbehaves on purpose.

    mode="drip":     sends a status line, then one header line every `interval` seconds,
                     forever (each read succeeds quickly, so per-read timeouts never fire)
    mode="stall":    accepts the TCP connection and never sends a byte (stalls TLS)
    mode="redirect": answers every request with 302 to itself
    """

    def __init__(self, mode, interval=0.2, lifetime=6.0):
        import socket

        self.mode = mode
        self.interval = interval
        self.lifetime = lifetime
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def url(self, path="/", scheme="http"):
        return f"{scheme}://127.0.0.1:{self.port}{path}"

    def _serve(self):
        self.sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        import time

        deadline = time.monotonic() + self.lifetime
        try:
            if self.mode == "stall":
                while not self._stop.is_set() and time.monotonic() < deadline:
                    time.sleep(0.05)
                return
            conn.settimeout(2)
            conn.recv(65536)
            self.requests += 1
            if self.mode == "redirect":
                conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: /again\r\nContent-Length: 0\r\n\r\n")
                return
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            while not self._stop.is_set() and time.monotonic() < deadline:
                conn.sendall(b"X-Drip: a\r\n")
                time.sleep(self.interval)
        except OSError:
            pass
        finally:
            conn.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.thread.join(2)
        self.sock.close()


class FakeWebhook(_Server):
    def __init__(self):
        super().__init__(_WebhookHandler)
        self.received = []
        self.lock = threading.Lock()

    def messages(self):
        with self.lock:
            return [body.get("content") or body.get("text") or "" for body in self.received]
