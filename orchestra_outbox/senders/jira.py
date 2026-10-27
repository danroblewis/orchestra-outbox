"""The Jira Data Center sender: ``jira.comment``, ``jira.transition`` and ``jira.create``, as the owner,
through REST API v2 (``/rest/api/2``) with a personal access token as ``Bearer``.

- ``jira.comment``: ``POST /issue/{key}/comment``, with an entity property ``orchestra-outbox``
  ``{"draft": <id>}`` on the comment (invisible), which ``find`` looks for
  (``GET /issue/{key}/comment?expand=properties``).
- ``jira.transition``: the content is the transition's name or id. ``GET /issue/{key}/transitions``
  resolves a name, ``POST /issue/{key}/transitions`` makes it. After a lost answer, ``find`` counts
  the transition as made when the issue no longer offers it (Jira offers a transition from the
  states it leaves, so one made is gone). That is an inference, said so in the remote
  (``"verified": "no_longer_offered"``).
- ``jira.create``: ``POST /issue`` with the kind's ``payload`` (the issue's fields), rendered from
  the draft's fields with ``$content`` the approved content, and ``target.project`` (when given) as
  the project. The new issue carries the property ``orchestra-outbox``; ``find`` searches the owner's
  issues created since the first try (``reporter = currentUser() AND created >= -Nm``) and reads
  each one's property."""

from __future__ import annotations

import math
import time
from pathlib import Path

from .. import credentials
from ..credentials import CredentialError
from ..templates import render_payload
from . import SendError
from .http import JSONClient

PROPERTY = "orchestra-outbox"
LOOKBACK_S = 120.0
PAGES = 10


class JiraSender:
    system = "jira"

    def __init__(self, conf, repo_root: Path, clock=time.time):
        self.conf, self.repo_root, self.clock = conf, repo_root, clock
        self._client: JSONClient | None = None

    def __repr__(self) -> str:
        return f"JiraSender({self.conf.api!r})"

    def client(self) -> JSONClient:
        if self._client is None:
            try:
                tok = credentials.token("jira", self.repo_root, self.conf.token_file, self.conf.api)
            except CredentialError:
                raise SendError("no_credentials") from None
            self._client = JSONClient(f"{self.conf.api}/rest/api/2", {
                "Authorization": f"Bearer {tok}", "Accept": "application/json", "User-Agent": "orchestra-outbox"})
        return self._client

    def _check(self, status: int) -> None:
        if status < 300:
            return
        if status == 401:
            self._client = None                         # read the token again next time
            raise SendError("jira:http_401")
        raise SendError(f"jira:http_{status}", permanent=True)

    def _prop(self, draft) -> list[dict]:
        return [{"key": PROPERTY, "value": {"draft": draft.id}}]

    def _transition(self, draft) -> str | None:
        """The id of the transition the content names, if the issue offers it now."""
        status, _, out = self.client().call("GET", f"issue/{draft.target['issue']}/transitions")
        self._check(status)
        want = draft.content.strip().lower()
        for t in (out or {}).get("transitions") or []:
            if str(t.get("id")) == draft.content.strip() or str(t.get("name", "")).lower() == want:
                return str(t["id"])
        return None

    def send(self, draft, kind) -> dict:
        issue = draft.target.get("issue")
        if kind.sender == "jira.comment":
            status, _, out = self.client().call("POST", f"issue/{issue}/comment",
                                                body={"body": draft.content, "properties": self._prop(draft)})
            self._check(status)
            return {"issue": issue, "comment": str((out or {}).get("id"))}
        if kind.sender == "jira.transition":
            tid = self._transition(draft)
            if tid is None:
                raise SendError("jira:transition_not_offered", permanent=True)
            status, _, _ = self.client().call("POST", f"issue/{issue}/transitions", body={"transition": {"id": tid}})
            self._check(status)
            return {"issue": issue, "transition": tid}
        fields = render_payload(kind.payload, {**draft.fields, "content": draft.content})
        if draft.target.get("project"):
            fields["project"] = {"key": draft.target["project"]}
        status, _, out = self.client().call("POST", "issue", body={"fields": fields, "properties": self._prop(draft)})
        self._check(status)
        return {"issue": (out or {}).get("key"), "id": (out or {}).get("id")}

    def find(self, draft, kind) -> dict | None:
        issue = draft.target.get("issue")
        if kind.sender == "jira.transition":
            return None if self._transition(draft) else {"issue": issue, "verified": "no_longer_offered"}
        if kind.sender == "jira.comment":
            for page in range(PAGES):
                status, _, out = self.client().call("GET", f"issue/{issue}/comment", query={
                    "expand": "properties", "orderBy": "-created", "startAt": page * 100, "maxResults": 100})
                self._check(status)
                comments = (out or {}).get("comments") or []
                for c in comments:
                    for p in c.get("properties") or []:
                        if p.get("key") == PROPERTY and (p.get("value") or {}).get("draft") == draft.id:
                            return {"issue": issue, "comment": str(c.get("id"))}
                if len(comments) < 100:
                    return None
            raise SendError("jira:find_too_many_pages")
        minutes = math.ceil((self.clock() - (draft.sending_since or draft.created_at) + LOOKBACK_S) / 60)
        status, _, out = self.client().call("GET", "search", query={
            "jql": f"reporter = currentUser() AND created >= -{minutes}m ORDER BY created DESC",
            "fields": "key", "maxResults": 50})
        self._check(status)
        for i in (out or {}).get("issues") or []:
            status, _, prop = self.client().call("GET", f"issue/{i['key']}/properties/{PROPERTY}")
            if status == 404:
                continue
            self._check(status)
            if ((prop or {}).get("value") or {}).get("draft") == draft.id:
                return {"issue": i["key"], "id": i.get("id")}
        return None
