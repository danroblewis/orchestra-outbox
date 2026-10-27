"""The machine ``outbox``: ``python -m orchestra_outbox run``, which ``orchestra run`` keeps running as
``outbox.sender`` in a project with an ``[outbox]`` section.

It announces itself, serves the drafting API and ``/metrics`` (``server.py``), and every half
second publishes the audit rows not yet on the bus (``audit.py``) and tries the drafts due to be
sent (``dispatch.py``). It re-reads ``[outbox]`` when orchestra.toml changes, so a kind the
Supervisor adds is draftable within a second; a configuration that fails the check is logged and
the last good one stands. It holds every write token, and only it does: the web server never
sends."""

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

from . import MACHINE, PACKAGE, audit, measures
from .config import Settings, settings
from .dispatch import Dispatcher
from .senders import build
from .server import Server
from .service import Outbox
from .store import Store

log = logging.getLogger("orchestra_outbox")
TICK_S = 0.5


def endpoint_file(project):
    return project.state_dir / "outbox" / "endpoint.json"


class Machine:
    def __init__(self, project, *, clock: Callable[[], float] = time.time, events: Events | None = None):
        self.project, self.clock = project, clock
        self.store = Store.for_project(project, clock)
        self.settings: Settings = settings(project.config.raw)
        self.events = events or Events.for_project(project, source=MACHINE)
        self.senders = build(self.settings, project.repo_root)
        self.outbox = Outbox(self.store, self.settings, clock)
        self.dispatcher = Dispatcher(self.store, self.settings, self.senders, clock)
        self.server: Server | None = None
        self._toml_mtime = self._mtime()

    def _mtime(self) -> float | None:
        try:
            return (self.project.repo_root / "orchestra.toml").stat().st_mtime
        except OSError:
            return None

    def reload(self) -> bool:
        """Re-read ``[outbox]`` if orchestra.toml changed; True when new settings took effect."""
        m = self._mtime()
        if m == self._toml_mtime:
            return False
        self._toml_mtime = m
        try:
            self.project.reload_config()
            new = settings(self.project.config.raw)
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
        return measures.text(self.store, sorted(self.settings.kinds), self.clock())

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
