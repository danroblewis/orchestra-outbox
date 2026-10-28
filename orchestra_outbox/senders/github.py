"""The GitHub sender: ``github.pr_comment``, ``github.label``, ``github.pr_open`` and
``github.issue_create``, as the owner, through the REST API
(a token that can write the repository's issues and pull requests).

- ``github.pr_comment``: ``POST /repos/{o}/{r}/issues/{n}/comments``. The body ends with a hidden
  mark, ``<!-- orchestra-outbox:<draft id> -->`` (HTML comments do not render), which is how
  ``find`` recognises a comment already posted (``GET .../comments?since=``) after a send whose
  answer was lost. The page says so beside every comment draft.
- ``github.label``: ``POST /repos/{o}/{r}/issues/{n}/labels`` with the one label. Adding a label the
  item has is a no-op, so the kind is idempotent and is simply sent again.
- ``github.pr_open``: ``POST /repos/{o}/{r}/pulls`` ``{title, head, base, body}``, the title from the
  draft's ``title`` field and the body its content, ending with the same hidden mark; ``find`` lists
  the repository's pull requests from ``head`` (``GET .../pulls?head=&state=all``) for the mark.

It never calls the endpoints that approve a pull request (a review with the event ``APPROVE``, or
submitting one): ``call`` refuses them before anything is sent (the owner's decision, ``actions.py``)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from .. import credentials
from ..credentials import CredentialError
from . import SendError
from .http import JSONClient

LOOKBACK_S = 120.0
PAGES = 10
_REVIEWS = re.compile(r"/pulls/[^/]+/reviews(/[^/]+(/events)?)?/?$")


def mark(draft_id: str) -> str:
    return f"<!-- orchestra-outbox:{draft_id} -->"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GitHubSender:
    system = "github"

    def __init__(self, conf, repo_root: Path):
        self.conf, self.repo_root = conf, repo_root
        self._client: JSONClient | None = None

    def __repr__(self) -> str:
        return f"GitHubSender({self.conf.api!r})"

    def client(self) -> JSONClient:
        if self._client is None:
            try:
                tok = credentials.token("github", self.repo_root, self.conf.token_file, self.conf.api)
            except CredentialError:
                raise SendError("no_credentials") from None
            self._client = JSONClient(self.conf.api, {
                "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "orchestra-outbox"})
        return self._client

    def _check(self, status: int, headers: dict) -> None:
        if status < 300:
            return
        if status == 401:
            self._client = None                         # read the token again next time
            raise SendError("github:http_401")
        if status == 403 and (headers.get("retry-after") or headers.get("x-ratelimit-remaining") == "0"):
            raise SendError("github:rate_limited", retry_after=float(headers.get("retry-after") or 60))
        raise SendError(f"github:http_{status}", permanent=True)

    def call(self, method: str, path: str, **kw):
        """Every request this sender makes. A review that approves is refused here, whatever asked."""
        body = kw.get("body")
        if method != "GET" and _REVIEWS.search("/" + path.split("?", 1)[0]) and (
                not isinstance(body, dict) or str(body.get("event", "")).upper() not in ("COMMENT", "REQUEST_CHANGES")):
            raise SendError("refused:pr_approval", permanent=True)
        return self.client().call(method, path, **kw)

    def _issue(self, draft) -> str:
        return f"repos/{draft.target['repo']}/issues/{draft.target['number']}"

    def send(self, draft, kind) -> dict:
        if kind.sender == "github.issue_create":
            return self._open_issue(draft)
        if kind.sender == "github.label":
            status, headers, _ = self.call("POST", f"{self._issue(draft)}/labels", body={"labels": [draft.content]})
            self._check(status, headers)
            return {"repo": draft.target["repo"], "number": draft.target["number"], "label": True}
        if kind.sender == "github.pr_open":
            t = draft.target
            status, headers, out = self.call("POST", f"repos/{t['repo']}/pulls", body={
                "title": str(draft.fields.get("title", "")).strip(), "head": t["head"], "base": t["base"],
                "body": f"{draft.content}\n\n{mark(draft.id)}"})
            self._check(status, headers)
            return {"repo": t["repo"], "number": out.get("number") if isinstance(out, dict) else None}
        status, headers, out = self.call("POST", f"{self._issue(draft)}/comments",
                                         body={"body": f"{draft.content}\n\n{mark(draft.id)}"})
        self._check(status, headers)
        return {"repo": draft.target["repo"], "number": draft.target["number"],
                "comment": out.get("id") if isinstance(out, dict) else None}

    def _open_issue(self, draft) -> dict:
        repo = draft.target["repo"]
        body = {"title": str(draft.fields.get("title", "")).strip(), "body": f"{draft.content}\n\n{mark(draft.id)}"}
        labels = [x.strip() for x in str(draft.fields.get("labels") or "").split(",") if x.strip()]
        if labels:
            body["labels"] = labels
        status, headers, out = self.call("POST", f"repos/{repo}/issues", body=body)
        self._check(status, headers)
        return {"repo": repo, "number": out.get("number") if isinstance(out, dict) else None}

    def _find_issue(self, draft) -> dict | None:
        repo = draft.target["repo"]
        since = _iso((draft.sending_since or draft.created_at) - LOOKBACK_S)
        for page in range(1, PAGES + 1):
            status, headers, out = self.call("GET", f"repos/{repo}/issues",
                                             query={"since": since, "state": "all", "per_page": 100, "page": page})
            self._check(status, headers)
            items = out if isinstance(out, list) else []
            for i in items:
                if mark(draft.id) in str(i.get("body") or ""):
                    return {"repo": repo, "number": i.get("number")}
            if len(items) < 100:
                return None
        raise SendError("github:find_too_many_pages")

    def find(self, draft, kind) -> dict | None:
        if kind.sender == "github.issue_create":
            return self._find_issue(draft)
        if kind.sender == "github.label":
            return None                                  # idempotent: sending again is the check
        if kind.sender == "github.pr_open":
            return self._find_pr(draft)
        since = _iso((draft.sending_since or draft.created_at) - LOOKBACK_S)
        for page in range(1, PAGES + 1):
            status, headers, out = self.call("GET", f"{self._issue(draft)}/comments",
                                             query={"since": since, "per_page": 100, "page": page})
            self._check(status, headers)
            items = out if isinstance(out, list) else []
            for c in items:
                if mark(draft.id) in str(c.get("body") or ""):
                    return {"repo": draft.target["repo"], "number": draft.target["number"], "comment": c.get("id")}
            if len(items) < 100:
                return None
        raise SendError("github:find_too_many_pages")

    def _find_pr(self, draft) -> dict | None:
        t = draft.target
        head = t["head"] if ":" in t["head"] else f"{t['repo'].split('/')[0]}:{t['head']}"
        for page in range(1, PAGES + 1):
            status, headers, out = self.call("GET", f"repos/{t['repo']}/pulls",
                                             query={"head": head, "state": "all", "per_page": 100, "page": page})
            self._check(status, headers)
            items = out if isinstance(out, list) else []
            for p in items:
                if mark(draft.id) in str(p.get("body") or ""):
                    return {"repo": t["repo"], "number": p.get("number")}
            if len(items) < 100:
                return None
        raise SendError("github:find_too_many_pages")
