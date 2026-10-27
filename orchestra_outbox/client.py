"""For a drafting machine written in Python: request a draft and read it back. Any other machine
speaks the same HTTP (``server.py``; README "Drafting").

::

    from orchestra_outbox.client import OutboxClient
    outbox = OutboxClient.for_project(project, drafter="worker/1")
    d = outbox.draft("review.request", target={"channel": "C0REVIEW"}, fields={"link": url, "reviewers": "@ana"},
                     causationid=ticket_event["id"], key=f"rr:{pr}")
    outbox.get(d["id"])["state"]

The address is ``ORCHESTRA_OUTBOX_URL`` when set, else what the running outbox wrote to
``<state>/outbox/endpoint.json``; the token is ``ORCHESTRA_EVENTS_TOKEN`` when set (the launcher
sets it), else the project's (``orchestra.endpoint.token``)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from orchestra import endpoint

from .machine import endpoint_file


class OutboxError(Exception):
    def __init__(self, status: int, error: str):
        super().__init__(f"{status}: {error}")
        self.status, self.error = status, error


class OutboxClient:
    def __init__(self, url: str, token: str, drafter: str, timeout: float = 30.0):
        self.url, self._token, self.drafter, self.timeout = url.rstrip("/"), token, drafter, timeout

    def __repr__(self) -> str:
        return f"OutboxClient({self.url!r}, drafter={self.drafter!r})"

    @classmethod
    def for_project(cls, project, drafter: str) -> OutboxClient:
        url = os.environ.get("ORCHESTRA_OUTBOX_URL")
        if not url:
            try:
                url = json.loads(endpoint_file(project).read_text())["url"]
            except (OSError, ValueError, KeyError):
                raise OutboxError(503, "the outbox is not running (no endpoint.json): is `orchestra run` up "
                                       "with [outbox] configured?") from None
        token = os.environ.get(endpoint.ENV_TOKEN) or endpoint.token(project)
        return cls(url, token, drafter)

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(f"{self.url}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self._token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error", "")
            except ValueError:
                err = ""
            raise OutboxError(e.code, err) from None

    def draft(self, kind: str, *, target: dict | None = None, content: str | None = None, fields: dict | None = None,
              recipients: list[str] | None = None, causationid: str | None = None, correlationid: str | None = None,
              key: str | None = None) -> dict:
        """Request a draft; returns it (``id``, ``state``, ...). The same ``key`` again returns the same draft."""
        body = {"drafter": self.drafter, "kind": kind, "target": target or {}}
        for k, v in (("content", content), ("fields", fields), ("recipients", recipients),
                     ("causationid", causationid), ("correlationid", correlationid), ("key", key)):
            if v is not None:
                body[k] = v
        return self._call("POST", "/drafts", body)["draft"]

    def get(self, draft_id: str) -> dict:
        return self._call("GET", f"/drafts/{draft_id}")["draft"]
