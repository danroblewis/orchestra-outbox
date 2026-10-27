"""The outbox's events: the contract, and publishing the audit log's rows on the bus.

Every change to a draft is a row in the audit log (``store.py``), written with the change; the
outbox machine publishes the rows not yet published, in order, each from the source of whoever
acted, at the time they acted, with a stable id (``outbox:<draft>:<n>``), so a crash between
publishing and marking repeats an event with the same id rather than losing it.

**Never content.** ``data`` carries ids, kinds, targets (conversation ids, ``owner/repo`` and a
number), codes and flags; never a draft's text, an edit, a label's name or the owner's note. The
canary tests hold this.

=========================  ======================  ===========================================================
type                       source                  data
=========================  ======================  ===========================================================
``outbox.draft.created``   the drafter             draft, kind, target, recipients, approval, chars
``outbox.draft.held``      ``outbox``              draft, kind, to (the drafter), reason (``rate_limit``)
``outbox.draft.approved``  the owner, or           draft, kind, to, by (``owner``/``policy``), edited
                           ``outbox`` (by policy)
``outbox.draft.rejected``  the owner               draft, kind, to, reason (a code), note (whether there is one)
``outbox.sent``            ``outbox``              draft, kind, to, remote (where it landed), attempts
``outbox.send_failed``     ``outbox``              draft, kind, to, error (a code), attempts
=========================  ======================  ===========================================================

Every event's ``subject`` is the draft's id. ``outbox.draft.created`` carries the drafter's
``causationid`` (the ticket's or event's that caused it) and ``correlationid``; each later event's
``causationid`` is the event it follows, and ``correlationid`` is kept. ``to`` addresses an event
to the drafting machine: the drafter listens for ``outbox.draft.rejected`` (and the others) whose
``data.to`` is its own source, and reads the owner's note, if any, from the outbox's API."""

from __future__ import annotations

from datetime import UTC, datetime

CREATED, HELD, APPROVED, REJECTED = "outbox.draft.created", "outbox.draft.held", "outbox.draft.approved", "outbox.draft.rejected"
SENT, SEND_FAILED = "outbox.sent", "outbox.send_failed"
TYPES = (CREATED, HELD, APPROVED, REJECTED, SENT, SEND_FAILED)

REASONS = ("content", "target", "not_needed", "duplicate", "timing", "other")   # why the owner rejected


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds")


def flush(store, events, limit: int = 200) -> int:
    """Publish the audit rows not yet on the bus; returns how many. A publish that fails stops the
    flush, and the rest wait for the next one."""
    n = 0
    for row in store.unpublished(limit):
        ext = {"subject": row["draft"], "time": _iso(row["at"]), "id": row["event_id"]}
        for k in ("causationid", "correlationid"):
            if row[k]:
                ext[k] = row[k]
        events.publish(row["type"], row["data"], source=row["source"], **ext)
        store.published(row["seq"])
        n += 1
    return n
