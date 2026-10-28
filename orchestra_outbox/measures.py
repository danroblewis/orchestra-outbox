"""What the outbox serves on ``/metrics``, in the Prometheus text format, read from its record each
time (so a restart neither resets nor double-counts a counter). The plugin declares the scrape
(``Plugin.collectors``); the series are measurements, and discovery decides what is a stock or a
flow (Orchestra's rules 6 and 7).

- ``outbox_drafts_waiting{kind}``: drafts waiting for the owner;
- ``outbox_oldest_draft_waiting_seconds``: how long the oldest of them has waited (0 when none);
- ``outbox_drafts_sending{kind}``: approved drafts not yet sent (being tried, or waiting to retry);
- ``outbox_drafts_created_total{kind}``, ``outbox_drafts_rejected_total{kind}``,
  ``outbox_drafts_sent_total{kind}``, ``outbox_drafts_failed_total{kind}``;
- ``outbox_drafts_approved_total{kind, by, edited}``: ``by`` is ``owner`` or ``policy``; the share
  with ``edited="true"`` is the owner's edit rate, the measured stand-in for trust;
- the rate limits' usage (``limits.py``), for Orchestra's graphs and the Supervisor:
  ``outbox_rate_limit{kind}`` and ``outbox_rate_limit_window_seconds{kind}`` (the limit and its
  window), and per limit in use ``outbox_rate_limit_sends{kind, recipient}`` (sends begun in the
  window now), ``outbox_rate_limit_backlog{kind, recipient}`` (drafts waiting for it) and
  ``outbox_rate_limit_unresolvable{kind, recipient}`` (1 when waiting will not clear it); for an
  external API's limit, ``outbox_api_rate_limit_backlog{system}`` and
  ``outbox_api_rate_limit_unresolvable{system}``."""

from __future__ import annotations

from . import limits
from .store import APPROVED, DRAFTED, FAILED, REJECTED, SENDING, SENT


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def text(store, settings, now: float) -> str:
    c = store.counts()
    seen = sorted(set(settings.kinds) | set(c["created"]))
    by_state = c["by_state"]
    lines: list[str] = []

    def series(name: str, typ: str, help_: str, rows: list[tuple[dict, float]]) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {typ}")
        for labels, v in rows:
            lab = ",".join(f'{k}="{_esc(str(x))}"' for k, x in labels.items())
            lines.append(f"{name}{{{lab}}} {v:g}" if lab else f"{name} {v:g}")

    def per_kind(states: tuple[str, ...]) -> list[tuple[dict, float]]:
        return [({"kind": k}, sum(by_state.get((k, s), 0) for s in states)) for k in seen]

    series("outbox_drafts_waiting", "gauge", "Drafts waiting for the owner's approval.", per_kind((DRAFTED,)))
    oldest = c["oldest_waiting"]
    series("outbox_oldest_draft_waiting_seconds", "gauge", "How long the oldest waiting draft has waited.",
           [({}, max(now - oldest, 0.0) if oldest else 0.0)])
    series("outbox_drafts_sending", "gauge", "Approved drafts not yet sent.", per_kind((APPROVED, SENDING)))
    series("outbox_drafts_created_total", "counter", "Drafts requested by machines.",
           [({"kind": k}, c["created"].get(k, 0)) for k in seen])
    series("outbox_drafts_approved_total", "counter", "Drafts approved, by the owner or by the kind's policy.",
           [({"kind": k, "by": by, "edited": str(ed).lower()}, n) for (k, by, ed), n in sorted(c["approved"].items())])
    series("outbox_drafts_rejected_total", "counter", "Drafts the owner rejected.", per_kind((REJECTED,)))
    series("outbox_drafts_sent_total", "counter", "Drafts sent.", per_kind((SENT,)))
    series("outbox_drafts_failed_total", "counter", "Drafts whose send failed for good.", per_kind((FAILED,)))
    limited = sorted((k for k in settings.kinds.values() if k.limit is not None), key=lambda k: k.name)
    series("outbox_rate_limit", "gauge", "A kind's limit: sends per recipient per window.",
           [({"kind": k.name}, k.limit) for k in limited])
    series("outbox_rate_limit_window_seconds", "gauge", "A kind's limit's window.", [({"kind": k.name}, k.per) for k in limited])
    status = limits.status(store, settings, now)
    mine = [lim for lim in status if lim.scope == "kind"]
    apis = [lim for lim in status if lim.scope == "api"]
    at = [({"kind": lim.kind, "recipient": lim.recipient}, lim) for lim in mine]
    series("outbox_rate_limit_sends", "gauge", "Sends begun in the limit's window now, per kind and recipient.",
           [(lab, lim.sent) for lab, lim in at])
    series("outbox_rate_limit_backlog", "gauge", "Approved drafts waiting for the limit's window.",
           [(lab, lim.waiting) for lab, lim in at])
    series("outbox_rate_limit_unresolvable", "gauge", "1 when waiting will not clear the limit's backlog.",
           [(lab, float(bool(lim.why))) for lab, lim in at])
    series("outbox_api_rate_limit_backlog", "gauge", "Drafts waiting for an external API's rate limit.",
           [({"system": lim.system}, lim.waiting) for lim in apis])
    series("outbox_api_rate_limit_unresolvable", "gauge", "1 when a draft waited past its patience for an API.",
           [({"system": lim.system}, float(bool(lim.why))) for lim in apis])
    return "\n".join(lines) + "\n"
