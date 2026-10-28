"""The machine ``outbox``: ``python -m orchestra_outbox run``, which ``orchestra run`` keeps running as
``outbox.sender`` in a project with an ``[outbox]`` section.

It announces itself, serves the drafting API, ``/limits`` and ``/metrics`` (``server.py``), and every
half second publishes the audit rows not yet on the bus (``audit.py``) and tries the drafts due to
be sent (``dispatch.py``); every few seconds it reviews the rate limits and has the drafters behind
an unresolvable one say so (``limits.py``). It re-reads ``[outbox]`` when orchestra.toml, the
private overlay or the shared accounts file changes, so a kind the Supervisor adds is draftable
within a second; a configuration that fails the check is logged and the last good one stands. It
holds every write token, and only it does: the web server never sends."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable

from orchestra import endpoint
from orchestra.config import ConfigError, load_project
from orchestra.events import Events

from . import MACHINE, PACKAGE, accounts, audit, limits, measures
from .config import Settings, settings
from .dispatch import Dispatcher
from .senders import build
from .server import Server
from .service import Outbox
from .store import Store

log = logging.getLogger("orchestra_outbox")
TICK_S = 0.5
REVIEW_S = 5.0


def endpoint_file(project):
    return project.state_dir / "outbox" / "endpoint.json"


class Machine:
    def __init__(self, project, *, clock: Callable[[], float] = time.time, events: Events | None = None):
        self.project, self.clock = project, clock
        self.store = Store.for_project(project, clock)
        self.settings: Settings = settings(project.config.raw, project.repo_root)
        self.events = events or Events.for_project(project, source=MACHINE)
        self.senders = build(self.settings, project.repo_root)
        self.outbox = Outbox(self.store, self.settings, clock)
        self.dispatcher = Dispatcher(self.store, self.settings, self.senders, clock)
        self.server: Server | None = None
        self._stamp = self._config_stamp()
        self._reviewed = 0.0

    def _config_stamp(self) -> tuple:
        try:
            shared = (accounts.directory() / accounts.FILE).stat().st_mtime_ns
        except OSError:
            shared = None
        return (self.project.config_stamp(), shared)

    def reload(self) -> bool:
        """Re-read ``[outbox]`` if orchestra.toml, the overlay or accounts.toml changed; True when new
        settings took effect."""
        stamp = self._config_stamp()
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        try:
            self.project.reload_config()
            new = settings(self.project.config.raw, self.project.repo_root)
        except (ConfigError, OSError, ValueError) as e:
            log.warning("outbox: orchestra.toml changed, but [outbox] does not pass the check; the last good "
                        "configuration stands: %s", e)
            return False
        if new.systems != self.settings.systems:
            self.senders = build(new, self.project.repo_root)
            self.dispatcher.senders = self.senders
        self.settings = self.outbox.settings = self.dispatcher.settings = new
        log.info("outbox: configuration reloaded: kinds %s", ", ".join(sorted(new.kinds)) or "none")
        return True

    def announce(self) -> None:
        log_file = os.environ.get("ORCHESTRA_MACHINE_LOG")
        self.events.publish("orchestra.machine.started", {
            "package": PACKAGE, "mode": "continuous", "kind": "program", "listens_for": [],
            **({"log": log_file} if log_file else {})})

    def metrics(self) -> str:
        return measures.text(self.store, self.settings, self.clock())

    def limits(self) -> dict:
        now = self.clock()
        return limits_view(self.store, self.settings, now)

    def review(self, force: bool = False) -> int:
        now = self.clock()
        if not force and now - self._reviewed < REVIEW_S:
            return 0
        self._reviewed = now
        return limits.review(self.store, self.settings, now)

    def flush(self) -> int:
        try:
            return audit.flush(self.store, self.events)
        except Exception as e:  # noqa: BLE001 - the bus being down must not stop sending; the rows wait
            log.warning("outbox: publishing the audit failed (%s); it will be tried again", type(e).__name__)
            return 0

    def step(self) -> None:
        self.reload()
        self.flush()
        self.dispatcher.step()
        self.review()
        self.flush()

    def serve(self) -> Server:
        self.server = Server(self, self.settings.host, self.settings.port, endpoint.token(self.project)).start()
        path = endpoint_file(self.project)
        path.write_text(json.dumps({"url": self.server.url, "pid": os.getpid(), "since": self.clock()}))
        return self.server

    def run(self, stop: threading.Event) -> None:
        self.announce()
        self.serve()
        log.info("outbox: serving %s; kinds %s", self.server.url, ", ".join(sorted(self.settings.kinds)) or "none")
        try:
            while not stop.is_set():
                try:
                    self.step()
                except Exception as e:  # noqa: BLE001 - the machine stays up; the next tick tries again
                    log.warning("outbox: a step failed: %s", type(e).__name__)
                stop.wait(TICK_S)
        finally:
            self.server.stop()
            try:
                endpoint_file(self.project).unlink()
            except FileNotFoundError:
                pass
            self.flush()


def limits_view(store: Store, s: Settings, now: float) -> dict:
    """The rate limits as ``/limits``, the page and ``orchestra outbox limits`` show them: each one in
    use, its status, and its usage over time (``limits.usage``) for a kind's limit."""
    out = []
    for lim in limits.status(store, s, now):
        v = lim.view()
        k = s.kinds.get(lim.kind) if lim.kind else None
        if k is not None:
            v["usage"] = limits.usage(store, k, lim.recipient, now)
        out.append(v)
    return {"now": now, "limits": out,
            "notices": [{"resource": r, "drafter": d, "since": t} for (r, d), t in sorted(store.notices().items())],
            "series": limits.SERIES}


def main(argv: list[str]) -> int:
    if argv[:1] != ["run"]:
        print("usage: python -m orchestra_outbox run", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    project = load_project()
    try:
        machine = Machine(project)
    except ConfigError as e:
        print(f"outbox: {e}", file=sys.stderr)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    machine.run(stop)
    log.info("outbox: stopped")
    return 0
