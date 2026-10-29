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


class FakeWebhook(_Server):
    def __init__(self):
        super().__init__(_WebhookHandler)
        self.received = []
        self.lock = threading.Lock()

    def messages(self):
        with self.lock:
            return [body.get("content") or body.get("text") or "" for body in self.received]
