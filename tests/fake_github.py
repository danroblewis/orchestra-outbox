"""A fake of GitHub's REST API, in the real shapes, for what the outbox's GitHub sender calls.

- ``POST /repos/{o}/{r}/issues/{n}/comments`` ``{"body"}``: 201 and the comment, as the token's
  owner; 404 for an unknown pull request; 422 ``Validation Failed`` for an empty body;
- ``GET /repos/{o}/{r}/issues/{n}/comments`` with ``since``, ``per_page`` and ``page``, oldest first;
- ``POST /repos/{o}/{r}/issues/{n}/labels`` ``{"labels": [...]}``: 200 and the item's labels
  (adding one it has changes nothing).

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

    def pr(self, n: int) -> int:
        self.prs.add(n)
        self.labels.setdefault(n, [])
        return n

    def comments_on(self, n: int) -> list[dict]:
        return [c for c in self.comments if c["issue_url"].endswith(f"/issues/{n}")]

    def route(self, method, path, q, body):
        m = _ISSUE.match(path)
        if not m or m.group(1) != self.repo or int(m.group(2)) not in self.prs:
            return 404, {"message": "Not Found", "documentation_url": "https://docs.github.com/rest", "status": "404"}, {}
        n, what = int(m.group(2)), m.group(3)
        headers = {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": "1793083020"}
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
