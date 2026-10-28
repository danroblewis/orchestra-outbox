"""A fake of GitHub's REST API, in the real shapes, for what the outbox's GitHub sender calls.

- ``POST /repos/{o}/{r}/issues/{n}/comments`` ``{"body"}``: 201 and the comment, as the token's
  owner; 404 for an unknown pull request; 422 ``Validation Failed`` for an empty body;
- ``GET /repos/{o}/{r}/issues/{n}/comments`` with ``since``, ``per_page`` and ``page``, oldest first;
- ``POST /repos/{o}/{r}/issues/{n}/labels`` ``{"labels": [...]}``: 200 and the item's labels
  (adding one it has changes nothing);
- ``POST /repos/{o}/{r}/pulls`` ``{title, head, base, body}``: 201 and the pull request; 422
  ``Validation Failed`` when one is open from that head already, or a field is missing;
- ``GET /repos/{o}/{r}/pulls`` with ``head`` (``owner:branch``), ``state``, ``per_page``, ``page``;
- ``POST /repos/{o}/{r}/issues`` ``{title, body, labels?}``: 201 and the issue (its number shared
  with the pull requests'); 422 ``Validation Failed`` without a title;
- ``GET /repos/{o}/{r}/issues`` with ``since``, ``state``, ``per_page``, ``page``;
- ``POST /repos/{o}/{r}/pulls/{n}/reviews``: recorded in ``reviews`` (the outbox must never call it).

It demands the bearer token (401 ``Bad credentials``), sends the rate-limit headers, and takes
``fail_next``, ``lose_next`` and ``asleep`` (``fake_http.py``). The shapes follow
``~/orchestra-bridges/tests/fake_github.py``, which its contract test keeps honest."""

from __future__ import annotations

import itertools
import re
import time
import zlib
from datetime import UTC, datetime

from fake_http import FakeServer

OWNER = "danroblewis"
API = "https://api.github.com"
_ISSUE = re.compile(r"^/repos/([^/]+/[^/]+)/issues/(\d+)/(comments|labels)$")
_PULLS = re.compile(r"^/repos/([^/]+/[^/]+)/pulls$")
_ISSUES = re.compile(r"^/repos/([^/]+/[^/]+)/issues$")
_REVIEWS = re.compile(r"^/repos/([^/]+/[^/]+)/pulls/(\d+)/reviews$")


def ts(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def user(login: str) -> dict:
    uid = zlib.crc32(login.encode()) % 10_000_000
    u = f"{API}/users/{login}"
    return {"login": login, "id": uid, "node_id": f"U_{uid}", "avatar_url": f"https://avatars.githubusercontent.com/u/{uid}?v=4",
            "gravatar_id": "", "url": u, "html_url": f"https://github.com/{login}", "type": "User",
            "user_view_type": "public", "site_admin": False}


class FakeGitHub(FakeServer):
    def __init__(self, repo: str = "acme/widgets"):
        super().__init__()
        self.repo = repo
        self.prs: set[int] = set()
        self.comments: list[dict] = []
        self.labels: dict[int, list[str]] = {}
        self._ids = itertools.count(5000)
        self.pulls: list[dict] = []
        self.reviews: list[dict] = []
        self.issues: list[dict] = []

    def pr(self, n: int) -> int:
        self.prs.add(n)
        self.labels.setdefault(n, [])
        return n

    def comments_on(self, n: int) -> list[dict]:
        return [c for c in self.comments if c["issue_url"].endswith(f"/issues/{n}")]

    def _pulls(self, method, q, body, headers):
        if method == "POST":
            b = body if isinstance(body, dict) else {}
            missing = [k for k in ("title", "head", "base") if not b.get(k)]
            head = f"{self.repo.split('/')[0]}:{b.get('head')}" if ":" not in str(b.get("head")) else b.get("head")
            if missing or any(p["head"]["label"] == head and p["state"] == "open" for p in self.pulls):
                return 422, {"message": "Validation Failed", "errors": [{"resource": "PullRequest", "code": "custom",
                             "message": f"A pull request already exists for {head}."}], "status": "422"}, headers
            n = max([*self.prs, 0]) + 1
            self.pr(n)
            now = ts(time.time())
            pull = {"url": f"{API}/repos/{self.repo}/pulls/{n}", "id": next(self._ids), "node_id": f"PR_{n}",
                    "html_url": f"https://github.com/{self.repo}/pull/{n}", "number": n, "state": "open",
                    "locked": False, "title": b["title"], "user": user(OWNER), "body": b.get("body"),
                    "created_at": now, "updated_at": now, "draft": False,
                    "head": {"label": head, "ref": head.split(":", 1)[1], "sha": "0" * 40},
                    "base": {"label": f"{self.repo.split('/')[0]}:{b['base']}", "ref": b["base"], "sha": "1" * 40},
                    "merged": False, "mergeable": None}
            self.pulls.append(pull)
            self.lose_if_asked()
            return 201, pull, {**headers, "Location": pull["url"]}
        items = [p for p in self.pulls if (not q.get("head") or p["head"]["label"] == q["head"])
                 and q.get("state", "open") in ("all", p["state"])]
        per, page = min(int(q.get("per_page", 30)), 100), int(q.get("page", 1))
        return 200, items[(page - 1) * per: page * per], headers

    def _issues(self, method, q, body, headers):
        if method == "POST":
            b = body if isinstance(body, dict) else {}
            if not str(b.get("title") or "").strip():
                return 422, {"message": "Validation Failed", "errors": [{"resource": "Issue", "code": "missing_field",
                             "field": "title"}], "status": "422"}, headers
            n = max([*self.prs, *(i["number"] for i in self.issues), 0]) + 1
            now = ts(time.time())
            issue = {"url": f"{API}/repos/{self.repo}/issues/{n}", "id": next(self._ids), "node_id": f"I_{n}",
                     "html_url": f"https://github.com/{self.repo}/issues/{n}", "number": n, "state": "open",
                     "title": b["title"], "body": b.get("body"), "user": user(OWNER),
                     "labels": [{"name": x, "color": "ededed", "default": False} for x in b.get("labels") or []],
                     "locked": False, "comments": 0, "created_at": now, "updated_at": now, "closed_at": None,
                     "author_association": "OWNER"}
            self.issues.append(issue)
            self.lose_if_asked()
            return 201, issue, {**headers, "Location": issue["url"]}
        since = q.get("since") or ""
        items = [i for i in self.issues if i["updated_at"] >= since and q.get("state", "open") in ("all", i["state"])]
        per, page = min(int(q.get("per_page", 30)), 100), int(q.get("page", 1))
        return 200, items[(page - 1) * per: page * per], headers

    def route(self, method, path, q, body):
        headers = {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": "1793083020"}
        if (m := _ISSUES.match(path)) and m.group(1) == self.repo:
            return self._issues(method, q, body, headers)
        if (m := _PULLS.match(path)) and m.group(1) == self.repo:
            return self._pulls(method, q, body, headers)
        if (m := _REVIEWS.match(path)) and m.group(1) == self.repo and method == "POST":
            self.reviews.append(body)
            return 200, {"id": next(self._ids), "state": str((body or {}).get("event", "PENDING"))}, headers
        m = _ISSUE.match(path)
        if not m or m.group(1) != self.repo or int(m.group(2)) not in self.prs:
            return 404, {"message": "Not Found", "documentation_url": "https://docs.github.com/rest", "status": "404"}, {}
        n, what = int(m.group(2)), m.group(3)
        if what == "comments" and method == "POST":
            text = (body or {}).get("body") if isinstance(body, dict) else None
            if not text:
                return 422, {"message": "Validation Failed", "errors": [{"resource": "IssueComment", "code": "missing_field",
                                                                          "field": "body"}], "status": "422"}, headers
            cid, now = next(self._ids), ts(time.time())
            issue_url = f"{API}/repos/{self.repo}/issues/{n}"
            c = {"url": f"{API}/repos/{self.repo}/issues/comments/{cid}",
                 "html_url": f"https://github.com/{self.repo}/pull/{n}#issuecomment-{cid}", "issue_url": issue_url,
                 "id": cid, "node_id": f"IC_{cid}", "user": user(OWNER), "created_at": now, "updated_at": now,
                 "body": text, "author_association": "MEMBER", "performed_via_github_app": None}
            self.comments.append(c)
            self.lose_if_asked()
            return 201, c, {**headers, "Location": c["url"]}
        if what == "comments" and method == "GET":
            since = q.get("since") or ""
            items = [c for c in self.comments_on(n) if c["updated_at"] >= since]
            per, page = min(int(q.get("per_page", 30)), 100), int(q.get("page", 1))
            return 200, items[(page - 1) * per: page * per], headers
        if what == "labels" and method == "POST":
            names = (body or {}).get("labels") if isinstance(body, dict) else None
            if not isinstance(names, list) or not names:
                return 422, {"message": "Validation Failed", "status": "422"}, headers
            for name in names:
                if name not in self.labels[n]:
                    self.labels[n].append(name)
            self.lose_if_asked()
            return 200, [{"id": zlib.crc32(x.encode()), "node_id": f"LA_{x}", "url": f"{API}/repos/{self.repo}/labels/{x}",
                          "name": x, "color": "ededed", "default": False, "description": None}
                         for x in self.labels[n]], headers
        return 404, {"message": "Not Found", "status": "404"}, headers
