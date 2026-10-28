"""The senders' tokens, from the owner's shared accounts directory (``accounts.py``, SPEC.md §9).

For each system, in order:

- ``<SYSTEM>_OUTBOX_TOKEN`` (``SLACK_``, ``GITHUB_``, ``JIRA_``), then the file
  ``<SYSTEM>_OUTBOX_TOKEN_FILE`` names: for tests and one-off runs;
- the file ``[outbox.<system>] token_file`` names;
- the shared ``~/.config/orchestra/tokens/<system>-token`` (``$ORCHESTRA_ACCOUNTS/tokens/...``),
  the place the bridges can read too;
- ``~/.config/orchestra-outbox/<system>-token``, where phase 1 kept them (still read; move it);
- for GitHub only, finally what ``gh auth token --hostname <the API's host>`` prints.

A file is refused if it is inside the repository, readable or writable by group or others (``chmod
600``), or empty. A token is never in orchestra.toml (``config.py`` refuses a ``token`` key).
``orchestra run`` passes its environment to every process it starts, so a token set in a variable
reaches every machine; a file does not.

A token is read when a send first needs it, kept in memory by its sender only, never logged,
published or written, and sent only to the configured API's own origin."""

from __future__ import annotations

import subprocess
import urllib.parse
from pathlib import Path

from . import accounts

LEGACY_DIR = Path("~/.config/orchestra-outbox")
ENV = {"slack": "SLACK_OUTBOX_TOKEN", "github": "GITHUB_OUTBOX_TOKEN", "jira": "JIRA_OUTBOX_TOKEN"}


class CredentialError(Exception):
    pass


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
    try:
        return accounts.token(system, env=(ENV[system],), token_file=token_file,
                              legacy=(LEGACY_DIR / f"{system}-token",), repo_root=repo_root)
    except accounts.AccountsError as e:
        if system != "github" or not str(e).startswith(f"no {system} token"):
            raise CredentialError(str(e)) from None
    return _gh(api)
