"""Rate limits: a draft over its limit is sent later, never dropped and never held for the owner; and
when the limit cannot be kept up with, the drafting machine says so on the bus (SPEC.md §5.3).

Two kinds of limit pace the sends:

- **a kind's** ``limit`` sends per recipient per ``per`` (``[outbox.kinds]``). The dispatcher asks
  ``wait`` before it first sends an approved draft; over the limit, the draft stays ``approved``,
  marked ``held: rate_limit``, and is tried when the window frees a place (``outbox.draft.held``
  says when). The window counts sends begun, whoever approved them;
- **an external API's** (Slack's ``ratelimited``, an HTTP 429, GitHub's rate limit): the send is tried
  again when the API says, ``held: api_rate_limit``, and a rate limit never uses up the draft's tries.

A **resource** is what a limit applies to: ``ratelimit:kind=<kind>,recipient=<id>`` or
``ratelimit:api=<system>``. It is **unresolvable** when waiting will not clear it:

- ``outpaced``: in the last window more drafts arrived than the limit lets out, and more wait than
  one window can send, so the backlog grows faster than the limit drains it;
- ``waited``: a draft has waited longer than its kind's ``patience`` (default twice ``per``, or an
  hour for a kind with no limit of its own).

Then each drafting machine with drafts in that backlog publishes ``outbox.rate_limit.unresolvable``
(``review``), once; when its backlog there is empty again, ``outbox.rate_limit.resolved``. The
notice is the drafter's, not the outbox's: the outbox is infrastructure and never a machine of its
own here (Orchestra's decision D19), and the drafter is the machine whose drafts are held and whose
drafting would have to change. ``status`` and ``usage`` give the same picture to the page, the
``/limits`` API, ``orchestra outbox limits`` and ``/metrics``."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

from . import audit as A
from .store import APPROVED, REJECTED, SENDING, SENT, Store

SLOT_S = 0.01                       # a place frees just after the oldest send leaves the window
MAX_IDS = 50
USAGE_POINTS = 48
SERIES = {"sends": "outbox_rate_limit_sends", "limit": "outbox_rate_limit", "backlog": "outbox_rate_limit_backlog",
          "unresolvable": "outbox_rate_limit_unresolvable", "api_backlog": "outbox_api_rate_limit_backlog"}


def kind_resource(kind: str, recipient: str) -> str:
    return f"ratelimit:kind={kind},recipient={recipient}"


def api_resource(system: str) -> str:
    return f"ratelimit:api={system}"


def parse(resource: str) -> dict:
    body = resource.removeprefix("ratelimit:")
    if body.startswith("api="):
        return {"scope": "api", "system": body[4:]}
    kind, _, recipient = body.removeprefix("kind=").partition(",recipient=")
    return {"scope": "kind", "kind": kind, "recipient": recipient}


def wait(c, store: Store, kind, draft, now: float) -> float:
    """Seconds until ``draft`` may be sent within its kind's limit; 0: now."""
    if kind.limit is None:
        return 0.0
    if kind.limit == 0:
        return kind.per                                  # never: look again in a window
    recent = [x for x in store.sent_since(c, kind.name, now - kind.per) if x.id != draft.id]
    until = now
    for r in draft.recipients:
        times = sorted(x.sending_since for x in recent if r in x.recipients)
        if len(times) >= kind.limit:
            until = max(until, times[len(times) - kind.limit] + kind.per + SLOT_S)
    return max(until - now, 0.0)


@dataclass
class Limit:
    resource: str
    scope: str                                   # "kind" or "api"
    system: str
    api: str | None
    kind: str | None = None
    recipient: str | None = None
    limit: int | None = None
    per: float | None = None
    patience: float | None = None
    sent: int = 0                                # sends begun in the last window
    arrivals: int = 0                            # drafts approved in the last window, sent or waiting
    backlog: dict[str, list[str]] = field(default_factory=dict)    # drafter: its drafts waiting, oldest first
    oldest_wait: float = 0.0
    why: list[str] = field(default_factory=list)

    @property
    def waiting(self) -> int:
        return sum(len(v) for v in self.backlog.values())

    def view(self) -> dict:
        return {"resource": self.resource, "scope": self.scope, "system": self.system, "api": self.api,
                "kind": self.kind, "recipient": self.recipient, "limit": self.limit, "per": self.per,
                "patience": self.patience, "sent_in_window": self.sent, "arrivals_in_window": self.arrivals,
                "backlog": self.waiting, "backlog_by_drafter": {k: len(v) for k, v in sorted(self.backlog.items())},
                "oldest_wait": round(self.oldest_wait, 3), "unresolvable": bool(self.why), "why": self.why}


def _host(settings, system: str) -> str | None:
    sy = settings.systems.get(system)
    return (urllib.parse.urlsplit(sy.api).hostname or None) if sy else None


def _kind_limits(store: Store, settings, k, now: float) -> list[Limit]:
    since = now - k.per
    drafts = store.of_kind(k.name, since)
    out: dict[str, Limit] = {}

    def at(r: str) -> Limit:
        if r not in out:
            out[r] = Limit(kind_resource(k.name, r), "kind", k.system, _host(settings, k.system), k.name, r, k.limit,
                           k.per, k.patience)
        return out[r]

    for d in sorted(drafts, key=lambda d: d.held_since or d.decided_at or d.created_at):
        for r in d.recipients:
            if d.state in (SENDING, SENT) and (d.sending_since or 0) >= since:
                at(r).sent += 1
            if d.state != REJECTED and d.decided_at and d.decided_at >= since:
                at(r).arrivals += 1
            if d.state == APPROVED and d.held == "rate_limit":
                lim = at(r)
                lim.backlog.setdefault(d.drafter, []).append(d.id)
                lim.oldest_wait = max(lim.oldest_wait, now - (d.held_since or now))
    for lim in out.values():
        if lim.waiting and lim.arrivals > k.limit and lim.waiting > k.limit:
            lim.why.append("outpaced")
        if lim.waiting and lim.oldest_wait > k.patience:
            lim.why.append("waited")
    return [out[r] for r in sorted(out)]


def _api_limits(store: Store, settings, now: float) -> list[Limit]:
    out: dict[str, Limit] = {}
    for d in sorted(store.held("api_rate_limit"), key=lambda d: d.held_since or 0):
        k = settings.kinds.get(d.kind)
        system = k.system if k else "unknown"
        lim = out.setdefault(system, Limit(api_resource(system), "api", system, _host(settings, system)))
        lim.backlog.setdefault(d.drafter, []).append(d.id)
        waited = now - (d.held_since or now)
        lim.oldest_wait = max(lim.oldest_wait, waited)
        patience = k.patience if k else 3600.0
        lim.patience = patience if lim.patience is None else min(lim.patience, patience)
        if waited > patience and "waited" not in lim.why:
            lim.why.append("waited")
    return [out[s] for s in sorted(out)]


def status(store: Store, settings, now: float) -> list[Limit]:
    """Every limit that is in use now: sending in its window, or with drafts waiting for it."""
    out: list[Limit] = []
    for k in sorted(settings.kinds.values(), key=lambda k: k.name):
        if k.limit is not None:
            out.extend(_kind_limits(store, settings, k, now))
    return out + _api_limits(store, settings, now)


def usage(store: Store, k, recipient: str, now: float, points: int = USAGE_POINTS) -> list[list[float]]:
    """``[[t, sends in the window ending at t, waiting at t], ...]`` over the last two windows (an
    hour to a week): the graph of a kind's limit for one recipient."""
    span = min(max(2 * k.per, 3600.0), 7 * 86_400.0)
    drafts = [d for d in store.of_kind(k.name, now - span - k.per) if recipient in d.recipients]
    rows = []
    for i in range(points + 1):
        t = now - span + span * i / points
        sends = sum(1 for d in drafts if d.state in (SENDING, SENT) and d.sending_since and t - k.per < d.sending_since <= t)
        waiting = sum(1 for d in drafts if d.state != REJECTED and d.held_since and d.held_since <= t
                      and (d.sending_since is None or d.sending_since > t))
        rows.append([round(t, 3), sends, waiting])
    return rows


def notice(lim: Limit, drafter: str) -> dict:
    """The ``data`` of ``outbox.rate_limit.unresolvable``: metadata only (SPEC.md §7.1)."""
    mine = lim.backlog.get(drafter, [])
    out = {"resource": lim.resource, "scope": lim.scope, "system": lim.system, "api": lim.api}
    if lim.scope == "kind":
        out |= {"kind": lim.kind, "recipient": lim.recipient}
    out |= {"limit": lim.limit, "per": lim.per, "patience": lim.patience, "sent_in_window": lim.sent,
            "arrivals_in_window": lim.arrivals if lim.scope == "kind" else None, "backlog": len(mine),
            "backlog_total": lim.waiting, "oldest_wait": round(lim.oldest_wait, 3), "why": list(lim.why),
            "drafts": mine[:MAX_IDS],
            "series": {k: v for k, v in SERIES.items() if (k == "api_backlog") == (lim.scope == "api")},
            "read": "/limits"}
    return out


def review(store: Store, settings, now: float) -> int:
    """Open a notice for each drafter with drafts behind an unresolvable limit, and close the ones
    whose backlog has cleared; returns how many events were recorded."""
    current = status(store, settings, now)
    by_resource = {lim.resource: lim for lim in current}
    opened = store.notices()
    n = 0
    with store.tx() as c:
        for lim in current:
            if not lim.why:
                continue
            for drafter in lim.backlog:
                if (lim.resource, drafter) in opened:
                    continue
                store.open_notice(c, lim.resource, drafter, now)
                store.audit(c, lim.resource, A.UNRESOLVABLE, drafter, notice(lim, drafter), at=now)
                n += 1
        for (resource, drafter), since in opened.items():
            lim = by_resource.get(resource)
            if lim is not None and lim.backlog.get(drafter):
                continue
            store.close_notice(c, resource, drafter)
            store.audit(c, resource, A.RESOLVED, drafter,
                        {"resource": resource, **parse(resource), "backlog": 0, "lasted": round(now - since, 3)},
                        causationid=store.last_event(c, resource, A.UNRESOLVABLE, source=drafter), at=now)
            n += 1
    return n
