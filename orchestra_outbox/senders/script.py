"""The script sender: a kind whose sender is ``script`` runs its ``command`` once the draft is
approved, for workflows too complicated for configuration (``arc feature``, a company tool, a chain
of API calls). The command is the kind's, in orchestra.toml; it lives in the target repository or
anywhere else, and is never plugin code.

The contract (SPEC.md §6.4):

- it runs in the repository root, with the outbox's environment plus ``OUTBOX_DRAFT_ID``,
  ``OUTBOX_KIND`` and ``OUTBOX_ATTEMPT`` (1 on the first try); a first argument with a ``/`` is a
  path relative to the repository root, anything else is looked up on ``PATH``;
- it reads the approved draft as one JSON object on stdin: ``id``, ``kind``, ``target``,
  ``recipients``, ``content`` (as approved), ``fields``, ``drafter``, ``causationid``,
  ``correlationid``, ``edited``, ``approved_by``, ``attempt``;
- exit 0: sent. Exit 75 (``EX_TEMPFAIL``) or no exit within the kind's ``timeout``: not known to be
  sent, so it is run again later with ``OUTBOX_ATTEMPT`` counting up. Any other exit: failed, for
  good;
- its output (stdout and stderr, the last 4 KB) is kept in the outbox's record beside the draft and
  shown on the page; only the exit status reaches the bus.

A script cannot be asked whether an earlier run landed, so **a script must be idempotent on the
draft's id**: a second run with the same ``OUTBOX_DRAFT_ID`` must not do the work twice."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

from . import SendError

TEMPFAIL = 75
OUTPUT_TAIL = 4096


def argv(command: tuple[str, ...], repo_root: Path) -> list[str]:
    first = command[0]
    if "/" in first:
        path = Path(first).expanduser()
        return [str(path if path.is_absolute() else repo_root / path), *command[1:]]
    found = shutil.which(first)
    return [found or first, *command[1:]]


class ScriptSender:
    system = "script"

    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)

    def __repr__(self) -> str:
        return "ScriptSender()"

    def send(self, draft, kind) -> dict:
        payload = {"id": draft.id, "kind": draft.kind, "target": draft.target, "recipients": draft.recipients,
                   "content": draft.content, "fields": draft.fields, "drafter": draft.drafter,
                   "causationid": draft.causationid, "correlationid": draft.correlationid, "edited": draft.edited,
                   "approved_by": draft.approved_by, "attempt": draft.attempts}
        env = {**os.environ, "OUTBOX_DRAFT_ID": draft.id, "OUTBOX_KIND": draft.kind, "OUTBOX_ATTEMPT": str(draft.attempts)}
        try:
            p = subprocess.Popen(argv(kind.command, self.repo_root), cwd=self.repo_root, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            raise SendError(f"script:cannot_start:{type(e).__name__}", permanent=True) from None
        try:
            out, _ = p.communicate(json.dumps(payload).encode(), timeout=kind.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, _ = p.communicate()
            raise SendError("script:timeout", output=self._tail(out)) from None
        tail = self._tail(out)
        if p.returncode == 0:
            return {"exit": 0, "_output": tail}
        raise SendError(f"script:exit_{p.returncode}", permanent=p.returncode != TEMPFAIL, output=tail)

    def find(self, draft, kind) -> dict | None:
        return None                 # a script cannot be asked; it is idempotent on the draft's id instead

    @staticmethod
    def _tail(out: bytes | None) -> str:
        return (out or b"")[-OUTPUT_TAIL:].decode("utf-8", "replace")
