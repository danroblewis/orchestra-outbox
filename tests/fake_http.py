"""What the fakes share: a local HTTP server on a free port, the bearer token demanded, scripted
failures, and a way to lose an answer after the request was carried out (the case a send must
never repeat).

Mirrors the fakes in ``~/orchestra-bridges/tests`` (``fake_github.py``, ``fake_slack.py`` on the
``slack-bridge`` branch), copied rather than imported: those are read-only and in another
repository, and the owner decided (2026-10-27) to keep such copies until it is decided what library
shared API code belongs in (SPEC §12, decision 5)."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self


class Lost(Exception):
    """Raised by a route after it acted, to drop the connection instead of answering."""


class FakeServer:
    TOKEN = "test-token"
    prefix = ""

    def __init__(self):
        self.requests: list[tuple[str, str, dict, object, str | None]] = []   # method, path, query, body, auth
        self.fail_next: list[tuple[int, dict, object]] = []                   # (status, headers, body) for the next requests
        self.lose_next = 0              # how many next writes are carried out, then their answer lost
        self.asleep = False             # every connection dropped, nothing done
        self._server: ThreadingHTTPServer | None = None
        self.base = ""
        self.lock = threading.RLock()

    # a fake answers (status, body, headers) from route(); raising Lost after acting drops the answer
    def route(self, method: str, path: str, q: dict, body: object) -> tuple[int, object, dict]:
        raise NotImplementedError

    def unauthorized(self) -> tuple[int, object, dict]:
        return 401, {"message": "Bad credentials"}, {}

    def start(self) -> Self:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, status: int, body, headers: dict | None = None):
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _drop(self):
                self.close_connection = True
                self.connection.close()

            def _handle(self, method: str):
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw.decode("utf-8", "replace")
                auth = self.headers.get("Authorization")
                path = u.path.removeprefix(fake.prefix)
                with fake.lock:
                    fake.requests.append((method, path, q, body, auth))
                    if fake.asleep:
                        return self._drop()
                    if fake.fail_next:
                        status, headers, fbody = fake.fail_next.pop(0)
                        return self._send(status, fbody, headers)
                    if auth != f"Bearer {fake.TOKEN}":
                        return self._send(*fake.unauthorized())
                    try:
                        status, out, headers = fake.route(method, path, q, body)
                    except Lost:
                        return self._drop()
                return self._send(status, out, headers)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def lose_if_asked(self) -> None:
        """Called by a write route after it acted."""
        if self.lose_next:
            self.lose_next -= 1
            raise Lost()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
