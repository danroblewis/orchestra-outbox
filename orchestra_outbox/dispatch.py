"""Sending approved drafts, at most once each.

A draft is claimed before its sender is called: ``approved`` becomes ``sending`` in the record, with
the time of the first try. Then:

- the sender answers: ``sent``, with where it landed;
- the system refuses it (``SendError.permanent``): ``failed``;
- anything else (no answer, a 5xx, a rate limit, a refused token, a timeout): it may or may not
  have landed, so it stays ``sending`` and is tried again after a pause that doubles (5 s up to
  5 min). **Every try after the first asks the sender to ``find`` the draft first**, by the mark it
  carries where it would have landed, and records it ``sent`` if found. A restart does the same:
  drafts left ``sending`` are tried again, ``find`` first. So a send is never repeated blindly.

After ``max_attempts`` tries the draft is ``failed``. Each outcome is an audit row, published by
the machine (``audit.py``)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from . import MACHINE
from . import audit as A
from .config import Settings
from .senders import SendError
from .store import FAILED, SENDING, SENT, Draft, Store

log = logging.getLogger("orchestra_outbox")
BACKOFF_S, BACKOFF_MAX_S = 5.0, 300.0


class Dispatcher:
    def __init__(self, store: Store, settings: Settings, senders: dict, clock: Callable[[], float] = time.time):
        self.store, self.settings, self.senders, self.clock = store, settings, senders, clock

    def step(self) -> int:
        """Try every draft that is due; returns how many were tried."""
        due = self.store.due(self.clock())
        for d in due:
            self.attempt(d)
        return len(due)

    def _claim(self, d: Draft) -> Draft | None:
        now = self.clock()
        with self.store.tx() as c:
            ok = self.store.move(c, d.id, (d.state,), state=SENDING, attempts=d.attempts + 1, next_attempt_at=None,
                                 sending_since=d.sending_since or now)
            return self.store.get(d.id, c) if ok else None

    def attempt(self, d: Draft) -> str:
        """One try at ``d``; returns its state after."""
        retry = d.state == SENDING                    # a try before this one may have landed
        kind = self.settings.kinds.get(d.kind)
        d = self._claim(d)
        if d is None:
            return "taken"
        if kind is None:
            return self._failed(d, SendError("kind_not_configured", permanent=True))
        sender = self.senders.get(kind.system)
        if sender is None:
            return self._failed(d, SendError(f"no_sender:{kind.system}", permanent=True))
        try:
            if retry and not kind.spec.idempotent and (found := sender.find(d, kind)) is not None:
                log.info("outbox: %s (%s) found already sent; not sending again", d.id, d.kind)
                return self._sent(d, found)
            remote = sender.send(d, kind)
        except SendError as e:
            if e.permanent:
                return self._failed(d, e)
            return self._later(d, e)
        except Exception as e:  # noqa: BLE001 - a sender's bug must not stop the machine; recorded as unknown
            log.warning("outbox: %s (%s): sender raised %s", d.id, d.kind, type(e).__name__)
            return self._later(d, SendError(f"sender_error:{type(e).__name__}"))
        return self._sent(d, remote)

    def _sent(self, d: Draft, remote: dict) -> str:
        now = self.clock()
        output = remote.pop("_output", None) if isinstance(remote, dict) else None
        with self.store.tx() as c:
            self.store.move(c, d.id, (SENDING,), state=SENT, sent_at=now, remote=remote, error=None, output=output)
            self.store.audit(c, d.id, A.SENT, MACHINE, {"draft": d.id, "kind": d.kind, "to": d.drafter,
                                                        "remote": remote, "attempts": d.attempts},
                             causationid=self.store.last_event(c, d.id, A.APPROVED), correlationid=d.correlationid, at=now)
        log.info("outbox: %s (%s) sent after %d attempt(s)", d.id, d.kind, d.attempts)
        return SENT

    def _failed(self, d: Draft, e: SendError) -> str:
        now = self.clock()
        with self.store.tx() as c:
            self.store.move(c, d.id, (SENDING,), state=FAILED, error=e.code, output=e.output, next_attempt_at=None)
            self.store.audit(c, d.id, A.SEND_FAILED, MACHINE, {"draft": d.id, "kind": d.kind, "to": d.drafter,
                                                               "error": e.code, "attempts": d.attempts},
                             causationid=self.store.last_event(c, d.id, A.APPROVED), correlationid=d.correlationid, at=now)
        log.warning("outbox: %s (%s) failed: %s", d.id, d.kind, e.code)
        return FAILED

    def _later(self, d: Draft, e: SendError) -> str:
        if d.attempts >= self.settings.max_attempts:
            return self._failed(d, SendError(f"gave_up:{e.code}", output=e.output))
        wait = min(BACKOFF_S * 2 ** (d.attempts - 1), BACKOFF_MAX_S)
        if e.retry_after:
            wait = max(wait, min(e.retry_after, BACKOFF_MAX_S * 3))
        with self.store.tx() as c:
            self.store.move(c, d.id, (SENDING,), next_attempt_at=self.clock() + wait, error=e.code, output=e.output)
        log.info("outbox: %s (%s) not known to be sent (%s); trying again in %.0f s", d.id, d.kind, e.code, wait)
        return SENDING

