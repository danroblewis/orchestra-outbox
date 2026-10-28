"""The owner's accounts: the tokens, the API addresses and the owner's own ids on each system, in one
place outside every repository, shared by the Outbox and, as they adopt it, the bridges.

The layout (SPEC.md §9)::

    ~/.config/orchestra/              0700; $ORCHESTRA_ACCOUNTS names another directory
      accounts.toml                   0600: who the owner is and their account on each system; no secret
      tokens/                         0700
        slack-token                   0600: one token per system, named as the bridges name theirs
        github-token
        jira-token
        jira_fedramp-token            a second Jira site, named as the bridges' [bridges.jira_fedramp]

``accounts.toml``::

    owner = "person.daniel"           # the owner's person id: person.<key> of the identity map

    [slack]
    account = "U012AB3CD"             # the owner's member id
    [github]
    api = "https://api.github.com"    # optional where the system has a default
    account = "danroblewis"
    [jira]
    api = "https://jira.example.com"
    account = "dlewis"

A system's table may carry other keys (a reader takes what it knows), so another program can keep
its own beside these. Neither file is ever in a repository or in ``orchestra.toml``: a file inside
the repository is refused, and so is one that group or others can read.

This module imports nothing of the outbox's, so it can move into a common library as it stands."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENV_DIR = "ORCHESTRA_ACCOUNTS"
DEFAULT_DIR = Path("~/.config/orchestra")
FILE = "accounts.toml"
TOKENS = "tokens"
_UNSAFE = re.compile(r"[^a-z0-9._-]")


class AccountsError(Exception):
    """A file that is missing, unsafe or malformed. The message names the file, never what it holds."""


def directory() -> Path:
    named = os.environ.get(ENV_DIR, "").strip()
    return Path(named).expanduser() if named else DEFAULT_DIR.expanduser()


def _inside(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def private_file(path: Path, repo_root: Path | None = None) -> str:
    """The text of an owner-only file outside the repository, stripped; raises when it is not one."""
    path = Path(path).expanduser()
    if _inside(path, repo_root):
        raise AccountsError(f"{path} is inside the repository: keep it outside")
    if not path.exists():
        raise AccountsError(f"no file at {path}")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise AccountsError(f"{path} is readable by others: chmod 600 it")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise AccountsError(f"{path} is empty")
    return text


def token_path(name: str) -> Path:
    """Where the shared token of ``name`` (``slack``, ``github``, ``jira``, ``jira_<site>``) lives."""
    return directory() / TOKENS / f"{name}-token"


def token(name: str, *, env: tuple[str, ...] = (), token_file: Path | None = None, legacy: tuple[Path, ...] = (),
          repo_root: Path | None = None) -> str:
    """``name``'s token. In order: each variable in ``env``; the file ``<VAR>_FILE`` names; ``token_file``
    (a program's own setting); the shared ``tokens/<name>-token``; then each ``legacy`` path that
    exists. Raises ``AccountsError``, naming where it looked, when there is none."""
    for var in env:
        if t := os.environ.get(var, "").strip():
            return t
    for var in env:
        if named := os.environ.get(f"{var}_FILE", "").strip():
            return private_file(Path(named), repo_root)
    if token_file is not None:
        return private_file(token_file, repo_root)
    shared = token_path(name)
    if shared.exists():
        return private_file(shared, repo_root)
    for p in legacy:
        if p.expanduser().exists():
            return private_file(p, repo_root)
    looked = [*env, str(shared), *(str(p) for p in legacy)]
    raise AccountsError(f"no {name} token (looked at {', '.join(looked)})")


@dataclass(frozen=True)
class Accounts:
    owner: str | None = None                          # the owner's person id
    systems: dict[str, dict] = field(default_factory=dict)
    path: Path | None = None                          # the file read, if any

    def api(self, system: str) -> str | None:
        v = self.systems.get(system, {}).get("api")
        return v if isinstance(v, str) and v else None

    def account(self, system: str) -> list[str]:
        """The owner's account ids on ``system`` (a table's ``account``, one id or a list)."""
        v = self.systems.get(system, {}).get("account")
        ids = v if isinstance(v, list) else [v] if v is not None else []
        return [str(x) for x in ids if isinstance(x, str | int) and str(x)]

    @classmethod
    def load(cls, where: Path | None = None) -> Accounts:
        """``accounts.toml`` in ``where`` (default: ``directory()``); empty when there is none."""
        path = Path(where or directory()) / FILE
        if not path.exists():
            return cls()
        try:
            raw = tomllib.loads(private_file(path))
        except tomllib.TOMLDecodeError as e:
            raise AccountsError(f"{path} is not valid TOML: line {e.lineno}") from None
        owner = raw.get("owner")
        if owner is not None and (not isinstance(owner, str) or not owner):
            raise AccountsError(f"{path}: owner must be a person id (person.<key>)")
        systems = {k: v for k, v in raw.items() if isinstance(v, dict)}
        for name, t in systems.items():
            api = t.get("api")
            if api is not None and not isinstance(api, str):
                raise AccountsError(f"{path}: [{name}] api must be a URL")
        return cls(owner, systems, path)


def person_in_map(map_path: Path | None, accounts: Accounts) -> str | None:
    """The owner's person id from the identity map the bridges read (``[people.<key>]`` tables naming
    an account per system): ``person.<key>`` of the one entry that lists one of the owner's accounts
    in ``accounts``. None when there is no map, no account, or no single match."""
    if map_path is None or not map_path.exists():
        return None
    try:
        people = tomllib.loads(map_path.read_text(encoding="utf-8")).get("people") or {}
    except (OSError, tomllib.TOMLDecodeError):
        return None
    found = set()
    for key, entry in people.items():
        if not isinstance(entry, dict) or entry.get("bot"):
            continue
        for system, listed in entry.items():
            if system in ("name", "team", "bot"):
                continue
            names = {str(a).lower() for a in (listed if isinstance(listed, list) else [listed])}
            if names & {a.lower() for a in accounts.account(system)}:
                found.add(key)
    if len(found) != 1:
        return None
    return "person." + (_UNSAFE.sub("-", found.pop().lower()) or "-")       # as the bridges' identity.py names it
