"""The outbox's settings: ``[outbox]`` in the project's orchestra.toml, a section Orchestra ignores.
The contract is in SPEC.md §4 and README.md; the Supervisor edits it, and ``orchestra-outbox check``
validates it first (``check.py``).

::

    [outbox]
    owner = "person.danroblewis"   # the source the owner's approvals publish as (default "owner")
    host = "127.0.0.1"             # where the outbox's API and /metrics listen (loopback only)
    port = 9467                    # 0: any free port, written to <state>/outbox/endpoint.json, no instrument

    [outbox.slack]                 # a system's connection, for its built-in senders
    api = "https://slack.com/api"
    token_file = "~/.config/orchestra-outbox/slack-token"

    [outbox.kinds."review.request"]
    sender = "slack.message"       # a built-in sender, or "script"
    template = "RR: $link cc: $reviewers"
    approval = "always"            # the default; "auto" only by an explicit entry
    limit = 1                      # sends of this kind per recipient ...
    per = "24h"                    # ... in this window; an auto draft over it waits for the owner

A token is never in this file: a ``token`` key is refused (``credentials.py`` says where they come
from). Every key of a kind is checked, and an unknown one is refused, so a typo is caught."""

from __future__ import annotations

import ipaddress
import re
import shlex
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestra.config import ConfigError, parse_duration

from . import DEFAULT_OWNER, MACHINE
from .kinds import SPECS, SYSTEMS, Spec
from .templates import placeholders

DEFAULT_PORT = 9467
APIS = {"slack": "https://slack.com/api", "github": "https://api.github.com", "jira": None}
_SOURCE = re.compile(r"^[A-Za-z0-9_.:-]+(/[A-Za-z0-9_.:-]+)?$")
_KIND = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9_-]+)*$")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KIND_KEYS = {"sender", "template", "payload", "command", "timeout", "target", "approval", "limit", "per"}
JIRA_CREATE_NEEDS = ("project", "issuetype", "summary")


@dataclass(frozen=True)
class Kind:
    name: str
    sender: str
    template: str | None = None       # renders the content from the draft's fields (and $content)
    payload: dict | None = None       # jira.create: the issue's fields, rendered at send time
    command: tuple[str, ...] = ()     # script: the command, run in the repository root
    timeout: float = 120.0            # script: seconds before it is stopped
    target: tuple[str, ...] = ()      # script: the target keys a draft must carry
    approval: str = "always"          # "always": the owner approves each one; "auto": sent unless over the limit
    limit: int | None = None          # sends per recipient in ``per``; None: no limit
    per: float = 86_400.0

    @property
    def spec(self) -> Spec:
        return SPECS[self.sender]

    @property
    def system(self) -> str:
        return self.spec.system


@dataclass(frozen=True)
class System:
    api: str
    token_file: Path | None = None


@dataclass
class Settings:
    owner: str = DEFAULT_OWNER
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    systems: dict[str, System] = field(default_factory=dict)
    kinds: dict[str, Kind] = field(default_factory=dict)
    max_attempts: int = 8


def section(raw: dict[str, Any]) -> dict | None:
    """``[outbox]`` of a parsed orchestra.toml, or None when the project does not configure it."""
    s = raw.get("outbox")
    return s if isinstance(s, dict) else None


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _api(system: str, value: object) -> str:
    url = APIS[system] if value is None else value
    if not isinstance(url, str) or not url:
        raise ConfigError(f"[outbox.{system}] api must be a URL" + (" (Jira has no default)" if system == "jira" else ""))
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme == "http" and _loopback(host):
        return url.rstrip("/")                     # a local fake
    if u.scheme != "https" or not host:
        raise ConfigError(f"[outbox.{system}] api must be https (or a loopback fake), not {url!r}")
    if system == "slack" and host != "slack.com" and not host.endswith(".slack.com"):
        raise ConfigError(f"[outbox.slack] api must be on slack.com (or a loopback fake), not {host!r}")
    return url.rstrip("/")


def _system(raw: dict, system: str) -> System | None:
    s = raw.get(system)
    if s is None:
        return None
    if not isinstance(s, dict):
        raise ConfigError(f"[outbox.{system}] must be a table")
    if "token" in s:
        raise ConfigError(f"[outbox.{system}] token is refused: a token never goes in orchestra.toml (use token_file)")
    unknown = set(s) - {"api", "token_file"}
    if unknown:
        raise ConfigError(f"[outbox.{system}] has unknown keys: {', '.join(sorted(unknown))} (it takes api, token_file)")
    tf = s.get("token_file")
    if tf is not None and not isinstance(tf, str):
        raise ConfigError(f"[outbox.{system}] token_file must be a path")
    return System(_api(system, s.get("api")), Path(tf).expanduser() if tf else None)


def _strings(where: str, value: object) -> None:
    """Every string in a payload has well-formed placeholders."""
    if isinstance(value, dict):
        for k, v in value.items():
            _strings(f"{where}.{k}", v)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _strings(f"{where}[{i}]", v)
    elif isinstance(value, str):
        try:
            placeholders(value)
        except ValueError as e:
            raise ConfigError(f"{where}: {e}") from None


def kind(name: str, s: object, systems: dict[str, System]) -> Kind:
    where = f'[outbox.kinds."{name}"]'
    if not _KIND.match(name):
        raise ConfigError(f"{where}: a kind's name is lowercase words joined by dots (review.request)")
    if not isinstance(s, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = set(s) - KIND_KEYS
    if unknown:
        raise ConfigError(f"{where} has unknown keys: {', '.join(sorted(unknown))} (it takes {', '.join(sorted(KIND_KEYS))})")
    sender = s.get("sender")
    if sender not in SPECS:
        raise ConfigError(f"{where} sender must be one of {', '.join(SPECS)}, not {sender!r}")
    spec = SPECS[sender]
    if spec.system != "script" and spec.system not in systems:
        raise ConfigError(f"{where} uses the sender {sender}, but [outbox.{spec.system}] is not configured")
    template = s.get("template")
    if template is not None:
        if not isinstance(template, str) or not template:
            raise ConfigError(f"{where} template must be a non-empty string")
        try:
            placeholders(template)
        except ValueError as e:
            raise ConfigError(f"{where} template: {e}") from None
    payload = s.get("payload")
    if spec.payload:
        if not isinstance(payload, dict):
            raise ConfigError(f"{where} needs a payload table: the issue's fields ({', '.join(JIRA_CREATE_NEEDS)}, ...)")
        missing = [k for k in JIRA_CREATE_NEEDS if k not in payload]
        if missing:
            raise ConfigError(f"{where} payload lacks {', '.join(missing)}")
        _strings(f"{where} payload", payload)
    elif payload is not None:
        raise ConfigError(f"{where} payload is only for jira.create")
    command: tuple[str, ...] = ()
    raw_cmd = s.get("command")
    if sender == "script":
        if isinstance(raw_cmd, str):
            raw_cmd = shlex.split(raw_cmd)
        if not isinstance(raw_cmd, list) or not raw_cmd or not all(isinstance(a, str) and a for a in raw_cmd):
            raise ConfigError(f"{where} needs command: a list of arguments (or a string split like a shell would)")
        command = tuple(raw_cmd)
    elif raw_cmd is not None:
        raise ConfigError(f"{where} command is only for the script sender")
    timeout = parse_duration(s.get("timeout"), 120.0)
    if not 1 <= timeout <= 3600:
        raise ConfigError(f"{where} timeout must be between 1s and 1h")
    target = s.get("target", [])
    if sender != "script" and "target" in s:
        raise ConfigError(f"{where} target is only for the script sender (a built-in sender knows its target)")
    if not isinstance(target, list) or not all(isinstance(k, str) and _KEY.match(k) for k in target):
        raise ConfigError(f"{where} target must be a list of key names")
    approval = s.get("approval", "always")
    if approval not in ("always", "auto"):
        raise ConfigError(f'{where} approval must be "always" or "auto"')
    limit = s.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
        raise ConfigError(f"{where} limit must be a whole number, 0 or more")
    per = parse_duration(s.get("per"), 86_400.0)
    if per < 60:
        raise ConfigError(f"{where} per must be at least 60s")
    return Kind(name, sender, template, payload, command, timeout, tuple(target), approval, limit, per)


def problems(raw: dict[str, Any]) -> tuple[Settings | None, list[str]]:
    """The settings, and every problem found (each a sentence); settings is None when any."""
    s = section(raw)
    if s is None:
        return None, ["orchestra.toml has no [outbox] section"]
    errors: list[str] = []
    unknown = set(s) - {"owner", "host", "port", "kinds", *SYSTEMS}
    if unknown:
        errors.append(f"[outbox] has unknown keys: {', '.join(sorted(unknown))}")
    owner = s.get("owner", DEFAULT_OWNER)
    if not isinstance(owner, str) or not _SOURCE.match(owner) or owner == MACHINE:
        errors.append(f"[outbox] owner must be a source name other than {MACHINE!r}")
    host = s.get("host", "127.0.0.1")
    if not isinstance(host, str) or not _loopback(host):
        errors.append("[outbox] host must be a loopback address: the API is for machines on this computer")
    port = s.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        errors.append("[outbox] port must be 0..65535")
    systems: dict[str, System] = {}
    for name in SYSTEMS:
        try:
            if (sy := _system(s, name)) is not None:
                systems[name] = sy
        except ConfigError as e:
            errors.append(str(e))
    raw_kinds = s.get("kinds", {})
    kinds: dict[str, Kind] = {}
    if not isinstance(raw_kinds, dict):
        errors.append("[outbox.kinds] must be a table of kinds")
        raw_kinds = {}
    for name, v in raw_kinds.items():
        try:
            kinds[name] = kind(name, v, systems)
        except ConfigError as e:
            errors.append(str(e))
    if errors:
        return None, errors
    return Settings(owner, host, port, systems, kinds), []


def settings(raw: dict[str, Any]) -> Settings:
    got, errors = problems(raw)
    if errors:
        raise ConfigError("; ".join(errors))
    return got


def valid_source(name: object) -> bool:
    return isinstance(name, str) and bool(_SOURCE.match(name))
