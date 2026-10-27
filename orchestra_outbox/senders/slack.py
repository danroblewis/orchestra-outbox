"""The Slack sender: ``slack.message`` and ``slack.reply``, posted as the owner with
``chat.postMessage`` (a user token, ``xoxp-``, with ``chat:write``; ``channels:history``,
``groups:history``, ``im:history`` and ``mpim:history`` for ``find``).

Each message carries Slack's message metadata, ``{"event_type": "orchestra_outbox_draft",
"event_payload": {"draft": <id>}}``: invisible in the channel, and how ``find`` recognises a message
already posted (``conversations.history``, or ``conversations.replies`` for a thread, with
``include_all_metadata``) after a send whose answer was lost."""

from __future__ import annotations

from pathlib import Path

from .. import credentials
from ..credentials import CredentialError
from . import SendError
from .http import JSONClient

EVENT_TYPE = "orchestra_outbox_draft"
LOOKBACK_S = 120.0       # how far before the first try find looks: clocks differ
PAGES = 10
# Slack's answers that mean the token, not the message, is wrong: the owner can fix them, so try again.
CREDENTIAL = {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive", "missing_scope",
              "no_permission"}
RETRY = {"ratelimited", "internal_error", "fatal_error", "service_unavailable", "request_timeout"}


class SlackSender:
    system = "slack"

    def __init__(self, conf, repo_root: Path):
        self.conf, self.repo_root = conf, repo_root
        self._client: JSONClient | None = None

    def __repr__(self) -> str:
        return f"SlackSender({self.conf.api!r})"

    def client(self) -> JSONClient:
        if self._client is None:
            try:
                tok = credentials.token("slack", self.repo_root, self.conf.token_file, self.conf.api)
            except CredentialError:
                raise SendError("no_credentials") from None
            self._client = JSONClient(self.conf.api, {"Authorization": f"Bearer {tok}",
                                                      "User-Agent": "orchestra-outbox"})
        return self._client

    def _ok(self, method: str, status: int, body: object, headers: dict) -> dict:
        if not isinstance(body, dict):
            raise SendError(f"slack:{method}:not_an_object")
        if body.get("ok"):
            return body
        err = str(body.get("error") or f"http_{status}")
        if err in CREDENTIAL:
            self._client = None                      # read the token again next time: the owner may fix it
            raise SendError(f"slack:{err}")
        if err in RETRY:
            raise SendError(f"slack:{err}", retry_after=float(headers.get("retry-after") or 0) or None)
        raise SendError(f"slack:{err}", permanent=True)

    def send(self, draft, kind) -> dict:
        body = {"channel": draft.target["channel"], "text": draft.content,
                "metadata": {"event_type": EVENT_TYPE, "event_payload": {"draft": draft.id}}}
        if kind.sender == "slack.reply":
            body["thread_ts"] = draft.target["thread_ts"]
        status, headers, out = self.client().call("POST", "chat.postMessage", body=body)
        out = self._ok("chat.postMessage", status, out, headers)
        return {"channel": out.get("channel") or draft.target["channel"], "ts": out.get("ts")}

    def find(self, draft, kind) -> dict | None:
        ch = draft.target["channel"]
        oldest = f"{(draft.sending_since or draft.created_at) - LOOKBACK_S:.6f}"
        if kind.sender == "slack.reply":
            method, q = "conversations.replies", {"channel": ch, "ts": draft.target["thread_ts"], "oldest": oldest}
        else:
            method, q = "conversations.history", {"channel": ch, "oldest": oldest}
        q.update(include_all_metadata="true", limit=200)
        cursor = ""
        for _ in range(PAGES):
            status, headers, out = self.client().call("GET", method, query={**q, **({"cursor": cursor} if cursor else {})})
            out = self._ok(method, status, out, headers)
            for m in out.get("messages") or []:
                meta = m.get("metadata") or {}
                if meta.get("event_type") == EVENT_TYPE and (meta.get("event_payload") or {}).get("draft") == draft.id:
                    return {"channel": ch, "ts": m.get("ts")}
            cursor = str((out.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                return None
        raise SendError("slack:find_too_many_pages")
