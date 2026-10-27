"""The senders' tokens: from the environment, or from an owner-only file outside the repository.

For each system, in order:

- Slack: ``SLACK_OUTBOX_TOKEN``; the file ``SLACK_OUTBOX_TOKEN_FILE`` names; the file
  ``[outbox.slack] token_file`` names; ``~/.config/orchestra-outbox/slack-token``.
- GitHub: ``GITHUB_OUTBOX_TOKEN``; the file ``GITHUB_OUTBOX_TOKEN_FILE`` names; the file
  ``[outbox.github] token_file`` names; ``~/.config/orchestra-outbox/github-token``; else what
  ``gh auth token --hostname <the API's host>`` prints (the owner's own login, as ``gh`` finds it).
- Jira (Data Center): a personal access token, sent as ``Bearer``: ``JIRA_OUTBOX_TOKEN``; the file
  ``JIRA_OUTBOX_TOKEN_FILE`` names; the file ``[outbox.jira] token_file`` names;
  ``~/.config/orchestra-outbox/jira-token``.

A file is refused if it is inside the repository (where it could be committed), readable or
writable by group or others (it must be ``chmod 600``), or empty. A token is never in
orchestra.toml (``config.py`` refuses a ``token`` key). The variables are a convenience for tests
and one-off runs: ``orchestra run`` passes its environment to every process it starts, so a token
set there reaches every machine; the file does not.

A token is read when a send first needs it, kept in memory by its sender only, never logged,
published or written, and sent only to the configured API's own origin."""

from __future__ import annotations

import os
import stat
import subprocess
import urllib.parse
from pathlib import Path

CONFIG_DIR = Path("~/.config/orchestra-outbox")
ENV = {"slack": "SLACK_OUTBOX_TOKEN", "github": "GITHUB_OUTBOX_TOKEN", "jira": "JIRA_OUTBOX_TOKEN"}


class CredentialError(Exception):
    pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def read_file(path: Path, repo_root: Path) -> str:
    path = path.expanduser()
    if _inside(path, repo_root):
        raise CredentialError(f"{path} is inside the repository: keep tokens outside it")
    if not path.exists():
        raise CredentialError(f"no token file at {path}")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialError(f"{path} is readable by others: chmod 600 it")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise CredentialError(f"{path} is empty")
    return token


def _gh(api: str) -> str:
    host = (urllib.parse.urlsplit(api).hostname or "").lower()
    host = "github.com" if host in ("api.github.com", "github.com") else host
    try:
        p = subprocess.run(["gh", "auth", "token", "--hostname", host], capture_output=True, text=True, timeout=30,
                           check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CredentialError(f"no GitHub token, and `gh auth token` could not run: {type(e).__name__}") from None
    if p.returncode != 0 or not p.stdout.strip():
        raise CredentialError(f"no GitHub token, and `gh auth token --hostname {host}` gave none")
    return p.stdout.strip()


def token(system: str, repo_root: Path, token_file: Path | None, api: str) -> str:
    """``system``'s token. Raises ``CredentialError``, naming where it looked, when there is none."""
    name = ENV[system]
    if t := os.environ.get(name, "").strip():
        return t
    named = os.environ.get(f"{name}_FILE")
    path = Path(named) if named else (token_file or CONFIG_DIR / f"{system}-token")
    if named or token_file or path.expanduser().exists() or system != "github":
        return read_file(path, repo_root)
    return _gh(api)
