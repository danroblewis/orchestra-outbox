"""The approval page's API, at ``/api/plugins/outbox``: **the human path that changes the system**,
which Orchestra's rule 9 allows a plugin to have. The owner reads the drafts and the audit log,
and approves (perhaps edited) or rejects a draft. Nothing here sends and nothing here holds a
token: the decision is written to the outbox's record, and the outbox machine publishes it and
sends (``machine.py``).

- ``GET /state``: the kinds and how each is approved (with the unedited approvals in a row, and a
  suggestion when a run is long), the drafts waiting, the latest decided, the rate limits in use
  with their usage over time, the audit log, and whether the outbox machine runs;
- ``POST /drafts/<id>/approve``, ``{"content": "..."}`` optional: approve, with the edit if given;
- ``POST /drafts/<id>/reject``, ``{"reason": <code>, "note": "..."}``: reject; the reason code goes
  on the bus to the drafter, the note only to the record, where the drafter reads it.

Every route is behind Orchestra's web login (its ``orchestra/web/auth.py``): a request gets here only
with the owner's session cookie or the web token as a bearer, and a write carried by the cookie
only from Orchestra's own origin. Both writes also need ``Content-Type: application/json``, which a
page of another site cannot send without the browser asking first: a second check, not the only
one."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request
from orchestra.config import ConfigError
from orchestra.runtime import alive

from . import audit as A
from .config import Settings, settings
from .kinds import DraftError
from .machine import endpoint_file, limits_view
from .service import Conflict, NotFound, Outbox
from .store import DRAFTED, Store

SUGGEST_AFTER = 10       # unedited approvals in a row after which the page says "could be automatic"


def router(project) -> APIRouter:
    r = APIRouter()
    opened: dict[str, Store] = {}

    def store() -> Store:
        if "s" not in opened:
            opened["s"] = Store.for_project(project)
        return opened["s"]

    def current() -> tuple[Settings | None, str | None]:
        try:
            project.reload_config()
            return settings(project.config.raw, project.repo_root), None
        except (ConfigError, OSError, ValueError) as e:
            return None, str(e)

    def running() -> bool:
        try:
            return bool(alive(json.loads(endpoint_file(project).read_text()).get("pid")))
        except (OSError, ValueError):
            return False

    async def body(request: Request) -> dict:
        if not (request.headers.get("content-type") or "").startswith("application/json"):
            raise HTTPException(415, "send JSON")
        try:
            b = await request.json()
        except ValueError:
            raise HTTPException(400, "the body is not JSON") from None
        if not isinstance(b, dict):
            raise HTTPException(400, "the body is a JSON object")
        return b

    def outbox() -> Outbox:
        s, err = current()
        if s is None:
            raise HTTPException(409, f"[outbox] is not configured or not valid: {err}")
        return Outbox(store(), s)

    @r.get("/state")
    def state() -> dict:
        s, err = current()
        st = store()
        streaks = st.streaks()
        kinds = [{"name": k.name, "sender": k.sender, "approval": k.approval, "limit": k.limit, "per": k.per,
                  "ping": k.ping, "action": k.action, "template": k.template is not None,
                  "unedited_streak": streaks.get(k.name, 0),
                  "suggest": k.approval == "always" and streaks.get(k.name, 0) >= SUGGEST_AFTER}
                 for k in (s.kinds.values() if s else [])]
        now = time.time()
        return {"ok": True, "now": now, "configured": s is not None, "config_error": err,
                "owner": s.owner if s else None, "owner_from": s.owner_from if s else None, "running": running(),
                "kinds": kinds, "suggest_after": SUGGEST_AFTER, "reasons": list(A.REASONS),
                "limits": limits_view(st, s, now) if s else None,
                "waiting": [d.view() for d in st.drafts((DRAFTED,))],
                "recent": [d.view() for d in st.decided(50)],
                "audit": st.log(150)}

    @r.post("/drafts/{draft_id}/approve")
    async def approve(draft_id: str, request: Request) -> dict:
        b = await body(request)
        try:
            d = outbox().approve(draft_id, b.get("content"))
        except NotFound:
            raise HTTPException(404, "no such draft") from None
        except Conflict as e:
            raise HTTPException(409, str(e)) from None
        except DraftError as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True, "draft": d.view()}

    @r.post("/drafts/{draft_id}/reject")
    async def reject(draft_id: str, request: Request) -> dict:
        b = await body(request)
        try:
            d = outbox().reject(draft_id, b.get("reason"), b.get("note"))
        except NotFound:
            raise HTTPException(404, "no such draft") from None
        except Conflict as e:
            raise HTTPException(409, str(e)) from None
        except DraftError as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True, "draft": d.view()}

    return r
