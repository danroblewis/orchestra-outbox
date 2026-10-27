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
  with ``edited="true"`` is the owner's edit rate, the measured stand-in for trust."""

from __future__ import annotations

from .store import APPROVED, DRAFTED, FAILED, REJECTED, SENDING, SENT


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def text(store, kinds: list[str], now: float) -> str:
    c = store.counts()
    seen = sorted(set(kinds) | set(c["created"]))
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
    return "\n".join(lines) + "\n"
