"""The senders: one per system, each sending the kinds of that system as the owner.

A sender has ``system``, ``send(draft, kind) -> remote`` and ``find(draft, kind) -> remote | None``
(``kind`` is the draft's configured kind: its template's payload, a script's command). ``send``
raises ``SendError``: ``permanent`` when the system answered that it will not take it (a channel
that does not exist, a pull request that is gone), so trying again is pointless; otherwise the send
may or may not have landed (a timeout, a 5xx, a rate limit, a refused token), and the dispatcher
calls ``find`` before it tries again, so a draft is never sent twice. ``find`` looks for the draft's
own mark where it would have landed (``SPEC.md`` §6)."""

from __future__ import annotations

from typing import Protocol


class SendError(Exception):
    def __init__(self, code: str, *, permanent: bool = False, retry_after: float | None = None,
                 output: str | None = None):
        super().__init__(code)
        self.code, self.permanent, self.retry_after = code, permanent, retry_after
        self.output = output              # a script's output: kept in the record, never on the bus


class Sender(Protocol):
    system: str

    def send(self, draft, kind) -> dict: ...

    def find(self, draft, kind) -> dict | None: ...


def build(settings, repo_root) -> dict[str, Sender]:
    """The senders of the systems ``settings`` configures, and the script sender. Nothing is read or
    opened yet: a token is read when a send first needs it."""
    from .github import GitHubSender
    from .jira import JiraSender
    from .script import ScriptSender
    from .slack import SlackSender

    made = {"slack": SlackSender, "github": GitHubSender, "jira": JiraSender}
    out: dict[str, Sender] = {name: made[name](system, repo_root) for name, system in settings.systems.items()}
    out["script"] = ScriptSender(repo_root)
    return out
