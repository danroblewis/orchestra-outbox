"""A fake of Slack's Web API, in the real shapes, for what the outbox's Slack sender calls.

- ``chat.postMessage`` (POST, a JSON body): posts as the token's owner, in a channel or a thread,
  keeping the message metadata; answers ``ok: false`` with Slack's errors for an unknown channel
  (``channel_not_found``), an unknown thread (``thread_not_found``), no text (``no_text``) or a text
  over 40,000 characters (``msg_too_long``);
- ``conversations.history`` and ``conversations.replies`` (GET): newest first (history) or oldest
  first (replies), filtered by ``oldest``, with ``metadata`` only when ``include_all_metadata``,
  paged by ``cursor`` and ``limit``.

A wrong token is ``200 {"ok": false, "error": "invalid_auth"}``, as Slack answers. ``fail_next``
answers the next requests as scripted; ``lose_next`` posts and then drops the answer; ``asleep``
drops everything. ``posted`` is what was posted, for the tests."""

from __future__ import annotations

import itertools
import time

from fake_http import FakeServer

OWNER = "U0OWNER"
TEAM = "T0FAKE"
CHANNEL, REVIEW = "C0GENERAL", "C0REVIEW"


class FakeSlack(FakeServer):
    TOKEN = "fake-slack-user-token-for-tests"
    prefix = "/api"

    def __init__(self):
        super().__init__()
        self.channels = {CHANNEL, REVIEW}
        self.messages: list[dict] = []          # every message, in order posted
        self._seq = itertools.count(1)

    @property
    def posted(self) -> list[dict]:
        return [m for m in self.messages if m["user"] == OWNER]

    def unauthorized(self):
        return 200, {"ok": False, "error": "invalid_auth"}, {}

    def say(self, channel: str, text: str, user: str = "U0OTHER", thread_ts: str | None = None) -> str:
        """Someone else posts (to start a thread the owner replies in); its ts."""
        ts = f"{int(time.time())}.{next(self._seq):06d}"
        self.messages.append({"channel": channel, "ts": ts, "user": user, "text": text, "thread_ts": thread_ts,
                              "metadata": None})
        return ts

    def _message(self, m: dict, meta: bool) -> dict:
        out = {"type": "message", "user": m["user"], "text": m["text"], "ts": m["ts"], "team": TEAM,
               "blocks": [{"type": "rich_text", "block_id": "b1", "elements": [
                   {"type": "rich_text_section", "elements": [{"type": "text", "text": m["text"]}]}]}]}
        if m["thread_ts"]:
            out["thread_ts"] = m["thread_ts"]
            if m["thread_ts"] != m["ts"]:
                out["parent_user_id"] = next((x["user"] for x in self.messages if x["ts"] == m["thread_ts"]), None)
        if meta and m["metadata"]:
            out["metadata"] = m["metadata"]
        return out

    def _page(self, items: list[dict], q: dict, meta: bool) -> dict:
        start, limit = int(q.get("cursor") or 0), min(int(q.get("limit") or 100), 1000)
        chunk = items[start:start + limit]
        more = start + limit < len(items)
        return {"ok": True, "messages": [self._message(m, meta) for m in chunk], "has_more": more,
                "pin_count": 0, "channel_actions_ts": None, "channel_actions_count": 0,
                "response_metadata": {"next_cursor": str(start + limit) if more else ""}}

    def route(self, method, path, q, body):
        name = path.lstrip("/")
        if name == "chat.postMessage" and method == "POST":
            body = body if isinstance(body, dict) else {}
            ch, text, thread = body.get("channel"), body.get("text"), body.get("thread_ts")
            if ch not in self.channels:
                return 200, {"ok": False, "error": "channel_not_found"}, {}
            if not text:
                return 200, {"ok": False, "error": "no_text"}, {}
            if len(text) > 40_000:
                return 200, {"ok": False, "error": "msg_too_long"}, {}
            if thread and not any(m["channel"] == ch and m["ts"] == thread for m in self.messages):
                return 200, {"ok": False, "error": "thread_not_found"}, {}
            ts = f"{int(time.time())}.{next(self._seq):06d}"
            m = {"channel": ch, "ts": ts, "user": OWNER, "text": text, "thread_ts": thread,
                 "metadata": body.get("metadata")}
            self.messages.append(m)
            self.lose_if_asked()
            return 200, {"ok": True, "channel": ch, "ts": ts, "message": self._message(m, True)}, {}
        if name in ("conversations.history", "conversations.replies") and method == "GET":
            ch = q.get("channel")
            if ch not in self.channels:
                return 200, {"ok": False, "error": "channel_not_found"}, {}
            oldest = float(q.get("oldest") or 0)
            meta = q.get("include_all_metadata") == "true"
            if name == "conversations.history":
                items = [m for m in self.messages if m["channel"] == ch and float(m["ts"]) > oldest
                         and (not m["thread_ts"] or m["thread_ts"] == m["ts"])]
                items.sort(key=lambda m: float(m["ts"]), reverse=True)
            else:
                root = q.get("ts")
                items = [m for m in self.messages if m["channel"] == ch and (m["ts"] == root or m["thread_ts"] == root)
                         and (m["ts"] == root or float(m["ts"]) > oldest)]
                if not items:
                    return 200, {"ok": False, "error": "thread_not_found"}, {}
                items.sort(key=lambda m: float(m["ts"]))
            return 200, self._page(items, q, meta), {}
        return 404, {"ok": False, "error": "unknown_method"}, {}
