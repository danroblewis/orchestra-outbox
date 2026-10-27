"""A fake of Jira Data Center's REST API v2 (``/rest/api/2``), in its shapes, for what the outbox's
Jira sender calls. Jira DC takes a personal access token as ``Bearer``.

- ``POST /issue/{key}/comment`` ``{"body", "properties"?}``: 201 and the comment;
- ``GET /issue/{key}/comment`` with ``expand=properties``, ``startAt``, ``maxResults``, ``orderBy``;
- ``GET /issue/{key}/transitions``: the transitions the issue's status offers;
- ``POST /issue/{key}/transitions`` ``{"transition": {"id"}}``: 204, or 400 when its status does
  not offer it;
- ``POST /issue`` ``{"fields", "properties"?}``: 201 ``{"id", "key", "self"}``, or 400 with
  ``errors`` per field (no summary, an unknown project or issue type);
- ``GET /search`` with a JQL of the form the sender asks (``reporter = currentUser() AND created >=
  -Nm``);
- ``GET /issue/{key}/properties/{name}``: 200 ``{"key", "value"}``, or 404.

A missing or wrong token is 401 with ``errorMessages``. Takes ``fail_next``, ``lose_next`` and
``asleep`` (``fake_http.py``)."""

from __future__ import annotations

import itertools
import re
import time
from datetime import UTC, datetime

from fake_http import FakeServer

OWNER = "dlewis"
BASE = "https://jira.example.com"
WORKFLOW = {   # status -> the transitions it offers: (id, name, to)
    "Open": [("11", "Start Progress", "In Progress"), ("31", "Done", "Done")],
    "In Progress": [("21", "Stop Progress", "Open"), ("31", "Done", "Done")],
    "Done": [("41", "Reopen", "Open")],
}
_ISSUE = re.compile(r"^/issue/([A-Z][A-Z0-9_]+-\d+)(/comment|/transitions|/properties/[^/]+)?$")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def person(name: str) -> dict:
    return {"self": f"{BASE}/rest/api/2/user?username={name}", "name": name, "key": f"JIRAUSER{len(name)}",
            "emailAddress": f"{name}@example.com", "displayName": name.title(), "active": True, "timeZone": "UTC"}


class FakeJira(FakeServer):
    TOKEN = "fake-jira-pat"
    prefix = "/rest/api/2"

    def __init__(self):
        super().__init__()
        self.projects = {"OPS": "10000"}
        self.issue_types = {"Task", "Bug", "Story"}
        self.issues: dict[str, dict] = {}
        self._ids = itertools.count(10100)
        self._n = {"OPS": itertools.count(1)}

    def unauthorized(self):
        return 401, {"errorMessages": ["You are not authenticated. Authentication required to perform this operation."],
                     "errors": {}}, {"WWW-Authenticate": "Bearer"}

    def issue(self, project: str = "OPS", status: str = "Open", reporter: str = "someone", fields: dict | None = None) -> str:
        key = f"{project}-{next(self._n[project])}"
        self.issues[key] = {"id": str(next(self._ids)), "key": key, "status": status, "reporter": reporter,
                            "created": time.time(), "fields": fields or {}, "comments": [], "properties": {}}
        return key

    def _err(self, status: int, messages: list[str], errors: dict | None = None):
        return status, {"errorMessages": messages, "errors": errors or {}}, {}

    def route(self, method, path, q, body):
        body = body if isinstance(body, dict) else {}
        if path == "/issue" and method == "POST":
            f = body.get("fields") or {}
            errors = {}
            if not f.get("summary"):
                errors["summary"] = "You must specify a summary of the issue."
            if (f.get("project") or {}).get("key") not in self.projects:
                errors["project"] = "project is required"
            if (f.get("issuetype") or {}).get("name") not in self.issue_types:
                errors["issuetype"] = "valid issue type is required"
            if errors:
                return self._err(400, [], errors)
            key = self.issue(f["project"]["key"], reporter=OWNER, fields=f)
            for p in body.get("properties") or []:
                self.issues[key]["properties"][p["key"]] = p["value"]
            self.lose_if_asked()
            i = self.issues[key]
            return 201, {"id": i["id"], "key": key, "self": f"{BASE}/rest/api/2/issue/{i['id']}"}, {}
        if path == "/search" and method == "GET":
            m = re.search(r"created >= -(\d+)m", q.get("jql", ""))
            if "reporter = currentUser()" not in q.get("jql", "") or not m:
                return self._err(400, ["The fake understands only the sender's JQL"])
            since = time.time() - int(m.group(1)) * 60
            found = sorted((i for i in self.issues.values() if i["reporter"] == OWNER and i["created"] >= since),
                           key=lambda i: i["created"], reverse=True)[: int(q.get("maxResults", 50))]
            return 200, {"expand": "schema,names", "startAt": 0, "maxResults": int(q.get("maxResults", 50)),
                         "total": len(found), "issues": [{"expand": "", "id": i["id"], "key": i["key"],
                                                          "self": f"{BASE}/rest/api/2/issue/{i['id']}", "fields": {}}
                                                         for i in found]}, {}
        m = _ISSUE.match(path)
        if not m or m.group(1) not in self.issues:
            return self._err(404, ["Issue Does Not Exist"])
        i, sub = self.issues[m.group(1)], m.group(2) or ""
        if sub == "/comment" and method == "POST":
            if not body.get("body"):
                return self._err(400, [], {"comment": "Comment body can not be empty!"})
            cid = str(next(self._ids))
            c = {"self": f"{BASE}/rest/api/2/issue/{i['id']}/comment/{cid}", "id": cid, "author": person(OWNER),
                 "body": body["body"], "updateAuthor": person(OWNER), "created": now(), "updated": now(),
                 "_properties": {p["key"]: p["value"] for p in body.get("properties") or []}}
            i["comments"].append(c)
            self.lose_if_asked()
            return 201, {k: v for k, v in c.items() if k != "_properties"}, {}
        if sub == "/comment" and method == "GET":
            items = list(reversed(i["comments"])) if q.get("orderBy") == "-created" else list(i["comments"])
            start, mx = int(q.get("startAt", 0)), int(q.get("maxResults", 50))
            out = []
            for c in items[start:start + mx]:
                v = {k: x for k, x in c.items() if k != "_properties"}
                if "properties" in (q.get("expand") or ""):
                    v["properties"] = [{"key": k, "value": x} for k, x in c["_properties"].items()]
                out.append(v)
            return 200, {"startAt": start, "maxResults": mx, "total": len(items), "comments": out}, {}
        if sub == "/transitions" and method == "GET":
            return 200, {"expand": "transitions", "transitions": [
                {"id": tid, "name": name, "to": {"self": f"{BASE}/rest/api/2/status/{to}", "description": "", "iconUrl": "",
                                                 "name": to, "id": str(abs(hash(to)) % 10000),
                                                 "statusCategory": {"id": 2, "key": "new", "name": "To Do"}}}
                for tid, name, to in WORKFLOW[i["status"]]]}, {}
        if sub == "/transitions" and method == "POST":
            tid = ((body.get("transition") or {}).get("id"))
            for t, name, to in WORKFLOW[i["status"]]:
                if t == tid:
                    i["status"] = to
                    self.lose_if_asked()
                    return 204, None, {}
            return self._err(400, [(f"It seems that you have tried to perform a workflow operation ({tid}) that is not "
                                   f"valid for the current state of this issue ({i['key']}).")])
        if sub.startswith("/properties/") and method == "GET":
            name = sub.removeprefix("/properties/")
            if name not in i["properties"]:
                return self._err(404, [f"The property with key '{name}' does not exist."])
            return 200, {"key": name, "value": i["properties"][name]}, {}
        return self._err(404, ["Not found"])
