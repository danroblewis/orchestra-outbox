"""The outbox's record: its drafts and its audit log, in SQLite under ``<state>/outbox/outbox.db``.

Two processes write it: the outbox machine (drafts, sends) and the web server (the owner's
decisions). Every write is one ``BEGIN IMMEDIATE`` transaction, and every change of state is
conditional on the state it leaves (``move``), so neither can overtake the other. The database is
the plugin's own state (AGENTS.md rule 5 of Orchestra); it holds the drafts' text, which is why it
lives in the state directory, readable by its owner only, and never on the bus.

The audit log is also the queue of the outbox's bus events: a row is written in the same
transaction as the change it records, with metadata only, and the machine publishes the rows it
has not published yet (``audit.py``). A change is therefore never on the bus without being
recorded, nor recorded without reaching the bus once the machine runs."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

DRAFTED, APPROVED, REJECTED, SENDING, SENT, FAILED = "drafted", "approved", "rejected", "sending", "sent", "failed"
STATES = (DRAFTED, APPROVED, REJECTED, SENDING, SENT, FAILED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, recipients TEXT NOT NULL,
  content TEXT NOT NULL, original TEXT NOT NULL, fields TEXT NOT NULL, drafter TEXT NOT NULL,
  causationid TEXT, correlationid TEXT, key TEXT,
  state TEXT NOT NULL, edited INTEGER NOT NULL DEFAULT 0, approved_by TEXT, held TEXT,
  reason TEXT, note TEXT, created_at REAL NOT NULL, decided_at REAL, sent_at REAL, remote TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL, sending_since REAL, error TEXT, output TEXT,
  UNIQUE (drafter, key)
);
CREATE INDEX IF NOT EXISTS drafts_state ON drafts (state);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, draft TEXT NOT NULL, at REAL NOT NULL, type TEXT NOT NULL,
  source TEXT NOT NULL, data TEXT NOT NULL, causationid TEXT, correlationid TEXT,
  event_id TEXT NOT NULL UNIQUE, published INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS audit_unpublished ON audit (published, seq);
CREATE TABLE IF NOT EXISTS merges (
  pr TEXT PRIMARY KEY, draft TEXT NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
  resource TEXT NOT NULL, drafter TEXT NOT NULL, opened_at REAL NOT NULL, PRIMARY KEY (resource, drafter)
);
"""
_ADDED = {"approver": "TEXT", "held_since": "REAL"}       # columns added after phase 1: added to an older record


@dataclass
class Draft:
    id: str
    kind: str
    target: dict
    recipients: list[str]
    content: str                      # what will be sent: the drafter's, or the owner's edit
    original: str                     # what the drafter's request rendered to
    fields: dict                      # the drafter's fields, for a payload rendered at send time
    drafter: str
    causationid: str | None = None
    correlationid: str | None = None
    key: str | None = None
    state: str = DRAFTED
    edited: bool = False
    approved_by: str | None = None    # "owner" or "policy"
    held: str | None = None           # why it waits: "rate_limit", "api_rate_limit" (sending later), "first_merge" (the owner)
    reason: str | None = None         # a rejection's reason code
    note: str | None = None           # the owner's words to the drafter (never on the bus)
    created_at: float = 0.0
    decided_at: float | None = None
    sent_at: float | None = None
    remote: dict | None = None        # where it landed: {"channel", "ts"}, {"comment"}, {"label"}
    attempts: int = 0
    next_attempt_at: float | None = None
    sending_since: float | None = None
    error: str | None = None          # the last send error's code, never a body
    output: str | None = None         # a script sender's output, its last 4 KB (here only, never on the bus)
    approver: str | None = None       # the owner's person id, when the owner approved or rejected it
    held_since: float | None = None   # when it first waited for a rate limit or a first merge

    def view(self) -> dict:
        """The draft as the page and the drafter's API show it, content included."""
        return asdict(self)


_JSON = ("target", "recipients", "remote", "fields")
_COLUMNS = list(Draft.__dataclass_fields__)


def _row(r: sqlite3.Row) -> Draft:
    d = dict(r)
    for k in _JSON:
        d[k] = json.loads(d[k]) if d[k] is not None else None
    d["edited"] = bool(d["edited"])
    return Draft(**d)


def new_id() -> str:
    return "d" + uuid.uuid4().hex[:15]


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fresh = not self.path.exists()
        self._local = threading.local()
        if fresh:                                   # owner-only before SQLite writes a byte (its -wal follows it)
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        c = self._conn()
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(_SCHEMA)
        have = {r[1] for r in c.execute("PRAGMA table_info(drafts)")}
        for col, typ in _ADDED.items():
            if col not in have:
                c.execute(f"ALTER TABLE drafts ADD COLUMN {col} {typ}")

    @classmethod
    def for_project(cls, project, clock: Callable[[], float] = time.time) -> Store:
        return cls(project.state_dir / "outbox" / "outbox.db", clock)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            self._local.conn = c
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # drafts

    def insert(self, c: sqlite3.Connection, d: Draft) -> None:
        vals = [json.dumps(getattr(d, k)) if k in _JSON else getattr(d, k) for k in _COLUMNS]
        c.execute(f"INSERT INTO drafts ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})", vals)

    def get(self, draft_id: str, c: sqlite3.Connection | None = None) -> Draft | None:
        r = (c or self._conn()).execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return _row(r) if r else None

    def by_key(self, c: sqlite3.Connection, drafter: str, key: str) -> Draft | None:
        r = c.execute("SELECT * FROM drafts WHERE drafter = ? AND key = ?", (drafter, key)).fetchone()
        return _row(r) if r else None

    def move(self, c: sqlite3.Connection, draft_id: str, frm: tuple[str, ...], **changes) -> bool:
        """Change a draft only if it is in one of ``frm``: True when it was."""
        sets = ", ".join(f"{k} = ?" for k in changes)
        vals = [json.dumps(v) if k in _JSON and v is not None else (int(v) if isinstance(v, bool) else v)
                for k, v in changes.items()]
        cur = c.execute(f"UPDATE drafts SET {sets} WHERE id = ? AND state IN ({', '.join('?' * len(frm))})",
                        [*vals, draft_id, *frm])
        return cur.rowcount == 1

    def drafts(self, states: tuple[str, ...] = STATES, limit: int = 500, newest_first: bool = False) -> list[Draft]:
        order = "DESC" if newest_first else "ASC"
        rows = self._conn().execute(
            f"SELECT * FROM drafts WHERE state IN ({', '.join('?' * len(states))}) ORDER BY created_at {order} LIMIT ?",
            [*states, limit]).fetchall()
        return [_row(r) for r in rows]

    def decided(self, limit: int = 50) -> list[Draft]:
        rows = self._conn().execute(
            "SELECT * FROM drafts WHERE state != ? ORDER BY COALESCE(sent_at, decided_at, created_at) DESC LIMIT ?",
            (DRAFTED, limit)).fetchall()
        return [_row(r) for r in rows]

    def due(self, now: float) -> list[Draft]:
        """Drafts the sender should try now: approved, or sending and due for another try."""
        rows = self._conn().execute(
            "SELECT * FROM drafts WHERE state IN (?, ?) AND COALESCE(next_attempt_at, 0) <= ? ORDER BY decided_at",
            (APPROVED, SENDING, now)).fetchall()
        return [_row(r) for r in rows]

    def sent_since(self, c: sqlite3.Connection, kind: str, since: float) -> list[Draft]:
        """Drafts of ``kind`` whose sending began since ``since`` and did not fail: what a limit counts."""
        rows = c.execute("SELECT * FROM drafts WHERE kind = ? AND sending_since >= ? AND state IN (?, ?)",
                         (kind, since, SENDING, SENT)).fetchall()
        return [_row(r) for r in rows]

    def of_kind(self, kind: str, since: float) -> list[Draft]:
        """Drafts of ``kind`` that were waiting or sent since ``since``, or are waiting now: what a
        limit's status and its usage over time are read from."""
        rows = self._conn().execute(
            "SELECT * FROM drafts WHERE kind = ? AND (state IN (?, ?) OR COALESCE(sending_since, held_since, decided_at, "
            "created_at) >= ? OR held_since >= ?)", (kind, APPROVED, SENDING, since, since)).fetchall()
        return [_row(r) for r in rows]

    def held(self, reason: str) -> list[Draft]:
        rows = self._conn().execute("SELECT * FROM drafts WHERE held = ? AND state IN (?, ?, ?)",
                                    (reason, DRAFTED, APPROVED, SENDING)).fetchall()
        return [_row(r) for r in rows]

    # merge actions: the first per pull request is the owner's

    def merged_before(self, c: sqlite3.Connection, pr: str) -> bool:
        return c.execute("SELECT 1 FROM merges WHERE pr = ?", (pr,)).fetchone() is not None

    def first_merge(self, c: sqlite3.Connection, pr: str, draft: str, at: float) -> None:
        c.execute("INSERT OR IGNORE INTO merges (pr, draft, at) VALUES (?, ?, ?)", (pr, draft, at))

    # the rate-limit notices open now, per resource and drafter

    def notices(self) -> dict[tuple[str, str], float]:
        return {(r, d): t for r, d, t in self._conn().execute("SELECT resource, drafter, opened_at FROM notices")}

    def open_notice(self, c: sqlite3.Connection, resource: str, drafter: str, at: float) -> None:
        c.execute("INSERT OR REPLACE INTO notices (resource, drafter, opened_at) VALUES (?, ?, ?)", (resource, drafter, at))

    def close_notice(self, c: sqlite3.Connection, resource: str, drafter: str) -> None:
        c.execute("DELETE FROM notices WHERE resource = ? AND drafter = ?", (resource, drafter))

    def counts(self) -> dict:
        """What /metrics serves, from the record: so a restart neither resets nor double-counts."""
        c = self._conn()
        out: dict = {"by_state": {}, "approved": {}, "created": {}, "oldest_waiting": None}
        for kind, state, n in c.execute("SELECT kind, state, COUNT(*) FROM drafts GROUP BY kind, state"):
            out["by_state"][(kind, state)] = n
        for kind, by, edited, n in c.execute(
                "SELECT kind, approved_by, edited, COUNT(*) FROM drafts WHERE approved_by IS NOT NULL "
                "GROUP BY kind, approved_by, edited"):
            out["approved"][(kind, by, bool(edited))] = n
        for kind, n in c.execute("SELECT kind, COUNT(*) FROM drafts GROUP BY kind"):
            out["created"][kind] = n
        r = c.execute("SELECT MIN(created_at) FROM drafts WHERE state = ?", (DRAFTED,)).fetchone()
        out["oldest_waiting"] = r[0]
        return out

    def streaks(self) -> dict[str, int]:
        """Per kind, how many of the owner's latest decisions in a row were unedited approvals: the
        measure an owner reads before giving a kind ``auto``."""
        out: dict[str, int] = {}
        rows = self._conn().execute(
            "SELECT kind, state, edited, approved_by FROM drafts WHERE decided_at IS NOT NULL "
            "AND (approved_by = 'owner' OR state = ?) ORDER BY decided_at DESC", (REJECTED,)).fetchall()
        broken: set[str] = set()
        for kind, state, edited, by in rows:
            out.setdefault(kind, 0)
            if kind in broken:
                continue
            if state != REJECTED and by == "owner" and not edited:
                out[kind] += 1
            else:
                broken.add(kind)
        return out

    # audit

    def audit(self, c: sqlite3.Connection, draft: str, type: str, source: str, data: dict, *,
              causationid: str | None = None, correlationid: str | None = None, at: float | None = None) -> str:
        """Record one change, metadata only, to be published on the bus as ``type`` from ``source``;
        returns its event id. ``draft`` is the event's subject: a draft's id, or a rate limit's
        resource (``limits.py``)."""
        n = c.execute("SELECT COUNT(*) FROM audit WHERE draft = ?", (draft,)).fetchone()[0]
        event_id = f"outbox:{draft}:{n + 1}"
        c.execute("INSERT INTO audit (draft, at, type, source, data, causationid, correlationid, event_id) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (draft, self.clock() if at is None else at, type, source, json.dumps(data), causationid,
                   correlationid, event_id))
        return event_id

    def last_event(self, c: sqlite3.Connection, draft: str, type: str | None = None,
                   source: str | None = None) -> str | None:
        q = ("SELECT event_id FROM audit WHERE draft = ?" + (" AND type = ?" if type else "")
             + (" AND source = ?" if source else "") + " ORDER BY seq DESC")
        args = [draft, *([type] if type else []), *([source] if source else [])]
        r = c.execute(q, args).fetchone()
        return r[0] if r else None

    def unpublished(self, limit: int = 200) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM audit WHERE published = 0 ORDER BY seq LIMIT ?", (limit,))
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def published(self, seq: int) -> None:
        self._conn().execute("UPDATE audit SET published = 1 WHERE seq = ?", (seq,))

    def log(self, limit: int = 200, draft: str | None = None) -> list[dict]:
        if draft:
            rows = self._conn().execute("SELECT * FROM audit WHERE draft = ? ORDER BY seq DESC LIMIT ?", (draft, limit))
        else:
            rows = self._conn().execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        return [{**dict(r), "data": json.loads(r["data"]), "published": bool(r["published"])} for r in rows]
