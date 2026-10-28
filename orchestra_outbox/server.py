"""The outbox machine's HTTP API, on ``[outbox] host:port`` (loopback only): how machines draft.

- ``POST /drafts``: a draft (SPEC.md §3); ``201`` with the draft when new, ``200`` when its ``key``
  was seen before (the same draft), ``400`` with the reason when it cannot be taken;
- ``GET /drafts/<id>``: the draft as it stands, with its content and the owner's note, for the
  drafter told by an event;
- ``GET /limits``: the rate limits in use, their backlog, whether each is unresolvable, and their
  usage over time (``limits.py``): what the Supervisor reads after ``outbox.rate_limit.unresolvable``;
- ``GET /metrics``: the outbox's measurements (``measures.py``), for the plugin's instrument;
- ``GET /health``.

The drafts and limits routes need ``Authorization: Bearer <the project's token>``, the token Orchestra hands
every process it launches (``ORCHESTRA_EVENTS_TOKEN``; ``orchestra events endpoint`` prints it for
others), so a process of another local user can neither draft nor read. ``/metrics`` and
``/health`` carry no content and need none. Requests are not logged: a body holds a draft's text."""

from __future__ import annotations

import hmac
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .kinds import DraftError

MAX_BODY = 1_000_000
_DRAFT = re.compile(r"^/drafts/([A-Za-z0-9]+)$")


class Server:
    def __init__(self, machine, host: str, port: int, token: str):
        self.machine, self.token = machine, token
        self._httpd = ThreadingHTTPServer((host, port), _handler(self))
        self._httpd.daemon_threads = True
        self.host, self.port = host, self._httpd.server_address[1]
        self.url = f"http://{host}:{self.port}"

    def start(self) -> Server:
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True,
                         name="outbox-api").start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def authorized(self, header: str | None) -> bool:
        scheme, _, given = (header or "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(given.strip().encode(), self.token.encode())


def _handler(srv: Server) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:        # never: a request may carry a draft's text
            pass

        def _send(self, status: int, body: dict | str, ctype: str = "application/json") -> None:
            raw = (json.dumps(body) if isinstance(body, dict) else body).encode()
            self.send_response(status)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth(self) -> bool:
            if srv.authorized(self.headers.get("Authorization")):
                return True
            self._send(401, {"ok": False, "error": "needs Authorization: Bearer <the project's token> "
                                                   "(ORCHESTRA_EVENTS_TOKEN, or `orchestra events endpoint`)"})
            return False

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                return self._send(200, {"ok": True})
            if path == "/metrics":
                return self._send(200, srv.machine.metrics(), "text/plain; version=0.0.4")
            if path == "/limits":
                return self._auth() and self._send(200, {"ok": True, **srv.machine.limits()})
            m = _DRAFT.match(path)
            if not m:
                return self._send(404, {"ok": False, "error": "not found"})
            if not self._auth():
                return None
            d = srv.machine.store.get(m.group(1))
            if d is None:
                return self._send(404, {"ok": False, "error": "no such draft"})
            return self._send(200, {"ok": True, "draft": d.view()})

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != "/drafts":
                return self._send(404, {"ok": False, "error": "not found"})
            if not self._auth():
                return None
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 < n <= MAX_BODY:
                return self._send(413 if n > MAX_BODY else 400, {"ok": False, "error": "a JSON body of at most 1 MB"})
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, {"ok": False, "error": "the body is not JSON"})
            try:
                d, new = srv.machine.outbox.request(body)
            except DraftError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            return self._send(201 if new else 200, {"ok": True, "new": new, "draft": d.view()})

    return Handler
