"""What can happen to a draft before it is sent: a machine requests it, the policy approves it (or
holds a first merge action for the owner), the owner approves (perhaps edited) or rejects it. Each
change is one transaction with its audit row (``store.py``, ``audit.py``). Sending, and the rate
limits that pace it, are ``dispatch.py``'s and ``limits.py``'s: a limit never holds a draft for the
owner."""

from __future__ import annotations

import time
from collections.abc import Callable

from . import MACHINE
from . import actions as X
from . import audit as A
from . import templates as T
from .config import Settings, valid_source
from .kinds import DraftError, check
from .store import APPROVED, DRAFTED, REJECTED, Draft, Store, new_id

MAX_REF = 200
MAX_NOTE = 4000
MAX_RECIPIENTS = 50


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    """The draft is no longer waiting: someone decided first."""


def _ref(body: dict, name: str) -> str | None:
    v = body.get(name)
    if v is None:
        return None
    if not isinstance(v, str) or not v or len(v) > MAX_REF:
        raise DraftError(f"{name} must be a string of at most {MAX_REF} characters")
    return v


def _not_an_approval(k, content: str) -> None:
    if why := X.content_approves(k.sender, content):
        raise DraftError(f"{k.name}: {why}, and no kind may approve a pull request (the owner's decision of 2026-10-27)")


class Outbox:
    def __init__(self, store: Store, settings: Settings, clock: Callable[[], float] = time.time):
        self.store, self.settings, self.clock = store, settings, clock

    def request(self, body: dict) -> tuple[Draft, bool]:
        """A machine's draft (``SPEC.md`` §3): the draft, and whether it is new (False: the drafter's
        ``key`` was seen before, and that draft is returned unchanged)."""
        if not isinstance(body, dict):
            raise DraftError("a draft is a JSON object")
        drafter = body.get("drafter")
        if not valid_source(drafter):
            raise DraftError("drafter must be the drafting machine's source")
        if drafter in (MACHINE, self.settings.owner):
            raise DraftError(f"{drafter!r} cannot draft: the outbox and the owner never draft for themselves")
        name = body.get("kind")
        k = self.settings.kinds.get(name) if isinstance(name, str) else None
        if k is None:
            known = ", ".join(sorted(self.settings.kinds)) or "none"
            raise DraftError(f"unknown kind {name!r}: this project's kinds are {known} ([outbox.kinds])")
        values = T.fields(body.get("fields"))
        content = body.get("content")
        if content is not None and not isinstance(content, str):
            raise DraftError("content must be a string")
        if k.template:
            content = T.render(k.template, {**values, "content": content or ""})
        elif content is None:
            raise DraftError(f"{k.name} has no template, so the draft needs content")
        target, content = check(k.spec, body.get("target", {}), content)
        missing = [t for t in k.target if t not in target]
        if missing:
            raise DraftError(f"{k.name} needs target keys {', '.join(missing)}")
        lacking = [f for f in k.spec.needs if not str(values.get(f, "")).strip()]
        if lacking:
            raise DraftError(f"{k.name} ({k.sender}) needs the fields {', '.join(lacking)}")
        _not_an_approval(k, content)
        if k.payload:
            T.render_payload(k.payload, {**values, "content": content})     # a field it lacks refuses it now
        recipients = body.get("recipients")
        if recipients is None and k.ping:
            raise DraftError(f"{k.name} is a ping: name the people it reaches in recipients (their ids on "
                             f"{k.system}), so its limit counts per person")
        if recipients is None:
            recipients = [k.spec.recipient(target)]
        if (not isinstance(recipients, list) or not 0 < len(recipients) <= MAX_RECIPIENTS
                or not all(isinstance(r, str) and 0 < len(r) <= MAX_REF for r in recipients)):
            raise DraftError(f"recipients must be a list of 1 to {MAX_RECIPIENTS} ids")
        cause, corr, key = _ref(body, "causationid"), _ref(body, "correlationid"), _ref(body, "key")
        policy = k
        now = self.clock()
        with self.store.tx() as c:
            if key and (seen := self.store.by_key(c, drafter, key)):
                return seen, False
            d = Draft(new_id(), k.name, target, sorted(set(recipients)), content, content, values, drafter, cause, corr, key,
                      created_at=now)
            self.store.insert(c, d)
            created = self.store.audit(c, d.id, A.CREATED, drafter, {
                "draft": d.id, "kind": d.kind, "sender": k.sender,
                **({} if k.sender == "script" else {"target": target}), "recipients": d.recipients,
                "approval": policy.approval, "chars": len(content)}, causationid=cause, correlationid=corr, at=now)
            if policy.approval == "auto" and not k.spec.owner_only:     # config refuses it too: never loosened here
                pr = X.merge_pr(k, target, content, self.settings.merge_labels)
                if pr and not self.store.merged_before(c, pr):
                    self.store.move(c, d.id, (DRAFTED,), held="first_merge", held_since=now)
                    self.store.audit(c, d.id, A.HELD, MACHINE, {"draft": d.id, "kind": d.kind, "to": drafter,
                                                                "reason": "first_merge", "pr": pr},
                                     causationid=created, correlationid=corr, at=now)
                else:
                    self.store.move(c, d.id, (DRAFTED,), state=APPROVED, approved_by="policy", decided_at=now)
                    self.store.audit(c, d.id, A.APPROVED, MACHINE, {"draft": d.id, "kind": d.kind, "to": drafter,
                                                                    "by": "policy", "edited": False},
                                     causationid=created, correlationid=corr, at=now)
            return self.store.get(d.id, c), True

    def _waiting(self, c, draft_id: str) -> Draft:
        d = self.store.get(draft_id, c)
        if d is None:
            raise NotFound(draft_id)
        if d.state != DRAFTED:
            raise Conflict(f"{draft_id} is {d.state}, not waiting")
        return d

    def approve(self, draft_id: str, content: str | None = None) -> Draft:
        """The owner approves: as drafted, or with ``content`` edited (validated as the kind's)."""
        now = self.clock()
        with self.store.tx() as c:
            d = self._waiting(c, draft_id)
            edited = content is not None and content != d.content
            k = self.settings.kinds.get(d.kind)
            if edited and k is None:
                raise DraftError(f"{d.kind} is no longer configured, so it cannot be edited")
            if edited and not isinstance(content, str):
                raise DraftError("content must be a string")
            text = k.spec.content(content) if edited else d.content
            edited = edited and text != d.content
            if k is not None:
                _not_an_approval(k, text)
                if pr := X.merge_pr(k, d.target, text, self.settings.merge_labels):
                    self.store.first_merge(c, pr, d.id, now)          # later merges of this PR follow the kind
            if not self.store.move(c, d.id, (DRAFTED,), state=APPROVED, content=text, edited=edited,
                                   approved_by="owner", approver=self.settings.owner, decided_at=now, held=None):
                raise Conflict(f"{draft_id} was decided meanwhile")
            self.store.audit(c, d.id, A.APPROVED, self.settings.owner,
                             {"draft": d.id, "kind": d.kind, "to": d.drafter, "by": "owner",
                              "approver": self.settings.owner, "edited": edited},
                             causationid=self.store.last_event(c, d.id, A.CREATED), correlationid=d.correlationid, at=now)
            return self.store.get(d.id, c)

    def reject(self, draft_id: str, reason: str, note: str | None = None) -> Draft:
        """The owner rejects, with a reason code for the bus and, perhaps, a note for the drafter alone."""
        if reason not in A.REASONS:
            raise DraftError(f"reason must be one of {', '.join(A.REASONS)}")
        if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
            raise DraftError(f"note must be a string of at most {MAX_NOTE} characters")
        note = (note or "").strip() or None
        now = self.clock()
        with self.store.tx() as c:
            d = self._waiting(c, draft_id)
            if not self.store.move(c, d.id, (DRAFTED,), state=REJECTED, reason=reason, note=note, decided_at=now,
                                   approver=self.settings.owner):
                raise Conflict(f"{draft_id} was decided meanwhile")
            self.store.audit(c, d.id, A.REJECTED, self.settings.owner,
                             {"draft": d.id, "kind": d.kind, "to": d.drafter, "reason": reason, "note": note is not None},
                             causationid=self.store.last_event(c, d.id, A.CREATED), correlationid=d.correlationid, at=now)
            return self.store.get(d.id, c)
