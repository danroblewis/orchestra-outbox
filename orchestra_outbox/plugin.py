"""The Outbox as an Orchestra plugin (``orchestra/plugins.py``): the approval page and its API, the
machine it declares, the process ``orchestra run`` keeps running for it, and the instrument that
reads its ``/metrics``, the last three only in a project whose orchestra.toml has ``[outbox]``.

Orchestra calls ``plugin(project)`` in the web server once, and in ``orchestra run`` each time
orchestra.toml changes, so it does no work and opens nothing: the router opens the record when a
request comes."""

from __future__ import annotations

import sys
from pathlib import Path

from orchestra.collect.spec import Collector
from orchestra.launcher import Process
from orchestra.plugins import Declared, Page, Plugin

from . import MACHINE, PACKAGE
from .config import DEFAULT_PORT, section

STATIC = Path(__file__).parent / "static"
STOP_GRACE_S = 15.0           # a send cut short stays "sending" and is found, or tried again, after a restart


def processes(project) -> list[Process]:
    if section(project.config.raw) is None:
        return []
    return [Process("sender", [sys.executable, "-m", "orchestra_outbox", "run"], stop_grace_s=STOP_GRACE_S)]


def collectors(project) -> list[Collector]:
    s = section(project.config.raw)
    if s is None:
        return []
    port = s.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= 65535:
        return []                                  # port 0 (any free port): nothing fixed to scrape
    host = s.get("host", "127.0.0.1")
    return [Collector.scrape("outbox", f"http://{host}:{port}/metrics", every_s=30)]


def plugin(project) -> Plugin:
    from .web import router  # FastAPI only where a router is wanted

    configured = section(project.config.raw) is not None
    return Plugin(
        PACKAGE,
        pages=[Page("/outbox", "Outbox", STATIC / "outbox.html")],
        static=STATIC,
        router=router(project),
        machines=[Declared(MACHINE, kind="program", mode="continuous")] if configured else [],
        processes=processes(project),
        collectors=collectors(project),
    )
