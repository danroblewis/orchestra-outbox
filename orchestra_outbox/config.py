"""The outbox's settings: ``[outbox]`` in the project's orchestra.toml, a section Orchestra ignores.
The contract is in SPEC.md §4 and README.md; the Supervisor edits it, and ``orchestra outbox check``
validates it first (``check.py``).

::

    [outbox]
    owner = "person.daniel"        # the owner's person id; else accounts.toml's, else the identity map's
    people = "people.toml"         # the identity map (the bridges'); default [bridges] people
    merge_labels = ["SAIL"]        # labels that merge a pull request (actions.py)
    host = "127.0.0.1"             # where the outbox's API and /metrics listen (loopback only)
    port = 9467                    # 0: any free port, written to <state>/outbox/endpoint.json, no instrument

    [outbox.slack]                 # a system's connection, for its built-in senders; its api and the
                                   # owner's account come from accounts.toml when not given here

    [outbox.kinds."review.request"]
    sender = "slack.message"       # a built-in sender, or "script"
    template = "RR: $link cc: $reviewers"
    approval = "always"            # the default; "auto" only by an explicit entry
    ping = true                    # it reaches people: a draft must name its recipients
    limit = 1                      # sends of this kind per recipient ...
    per = "24h"                    # ... in this window; a draft over it waits for the window, never dropped
    patience = "48h"               # how long a draft may wait for a limit before the outbox says so

A token is never in this file: a ``token`` key is refused (``credentials.py`` and ``accounts.py``
say where they come from). Every key of a kind is checked, and an unknown one is refused, so a typo
is caught."""

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
from . import actions as X
from .accounts import Accounts, AccountsError, person_in_map
from .kinds import SPECS, SYSTEMS, Spec
from .templates import placeholders

DEFAULT_PORT = 9467
APIS = {"slack": "https://slack.com/api", "github": "https://api.github.com", "jira": None}
_SOURCE = re.compile(r"^[A-Za-z0-9_.:-]+(/[A-Za-z0-9_.:-]+)?$")
_KIND = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9_-]+)*$")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KIND_KEYS = {"sender", "template", "payload", "command", "timeout", "target", "approval", "limit", "per", "ping",
             "action", "patience"}
TOP_KEYS = {"owner", "people", "merge_labels", "host", "port", "kinds", *SYSTEMS}
API_PATIENCE_S = 3600.0
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
    ping: bool = False                # it reaches people: a draft must name its recipients
    action: str = ""                  # what it does (actions.py): the sender's, or a script's declared one
    patience: float = 0.0             # how long a draft may wait for a rate limit before it is unresolvable

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
    owner_from: str = "default"       # where the owner's id came from: config, accounts, identity map, default
    merge_labels: tuple[str, ...] = ()


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


def _api(system: str, value: object, accounts: Accounts) -> str:
    url = value if value is not None else accounts.api(system) or APIS[system]
    if not isinstance(url, str) or not url:
        raise ConfigError(f"[outbox.{system}] api must be a URL (Jira has no default: set [jira] api in "
                          f"{accounts.path or 'the shared accounts.toml'}, or api here)" if system == "jira" else
                          f"[outbox.{system}] api must be a URL")
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme == "http" and _loopback(host):
        return url.rstrip("/")                     # a local fake
    if u.scheme != "https" or not host:
        raise ConfigError(f"[outbox.{system}] api must be https (or a loopback fake), not {url!r}")
    if system == "slack" and host != "slack.com" and not host.endswith(".slack.com"):
        raise ConfigError(f"[outbox.slack] api must be on slack.com (or a loopback fake), not {host!r}")
    return url.rstrip("/")


def _system(raw: dict, system: str, accounts: Accounts) -> System | None:
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
    return System(_api(system, s.get("api"), accounts), Path(tf).expanduser() if tf else None)


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
        if X.approves_in_code(" ".join(command)):
            raise ConfigError(f"{where} command approves a pull request, which no kind may do (the owner's decision "
                              "of 2026-10-27)")
    elif raw_cmd is not None:
        raise ConfigError(f"{where} command is only for the script sender")
    action = s.get("action")
    if sender == "script":
        if action not in X.SCRIPT_ACTIONS:
            raise ConfigError(f"{where} needs action: what the script does, one of {', '.join(X.SCRIPT_ACTIONS)}")
    elif action is not None:
        raise ConfigError(f"{where} action is only for the script sender (a built-in sender's action is known)")
    else:
        action = spec.action
    if action in X.FORBIDDEN:
        raise ConfigError(f"{where} approves a pull request, which no kind may do (the owner's decision of 2026-10-27)")
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
    if approval == "auto" and spec.owner_only:
        raise ConfigError(f'{where} approval must be "always": {sender} is public and speaks as the owner, so the '
                          "owner approves each one (the owner's decision of 2026-10-28)")
    limit = s.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
        raise ConfigError(f"{where} limit must be a whole number, 0 or more")
    per = parse_duration(s.get("per"), 86_400.0)
    if per < 60:
        raise ConfigError(f"{where} per must be at least 60s")
    if action == X.PR_MERGE:
        target = list(dict.fromkeys([*target, "repo", "number"]))     # the pull request it merges
    ping = s.get("ping", False)
    if not isinstance(ping, bool):
        raise ConfigError(f"{where} ping must be true or false")
    patience = parse_duration(s.get("patience"), 2 * per if limit is not None else API_PATIENCE_S)
    if patience < 60:
        raise ConfigError(f"{where} patience must be at least 60s")
    return Kind(name, sender, template, payload, command, timeout, tuple(target), approval, limit, per, ping, action,
                patience)


def people_file(raw: dict[str, Any], repo_root: Path | None) -> Path | None:
    """The identity map: ``[outbox] people``, else the bridges' ``[bridges] people``; relative to the
    repository unless absolute."""
    s, b = section(raw) or {}, raw.get("bridges") if isinstance(raw.get("bridges"), dict) else {}
    p = s.get("people") or b.get("people")
    if not isinstance(p, str) or not p:
        return None
    path = Path(p).expanduser()
    return path if path.is_absolute() or repo_root is None else Path(repo_root) / path


def _owner(s: dict, raw: dict, repo_root: Path | None, accounts: Accounts) -> tuple[object, str]:
    """The owner's person id and where it came from: ``[outbox] owner``; the shared accounts.toml's
    ``owner``; the identity map's entry listing one of the owner's accounts; else the default."""
    if "owner" in s:
        return s["owner"], "config"
    if accounts.owner:
        return accounts.owner, "accounts"
    if found := person_in_map(people_file(raw, repo_root), accounts):
        return found, "identity map"
    return DEFAULT_OWNER, "default"


NO_SECTION = "orchestra.toml has no [outbox] section"


def problems(raw: dict[str, Any], repo_root: Path | None = None,
             accounts: Accounts | None = None) -> tuple[Settings | None, list[str]]:
    """The settings, and every problem found (each a sentence); settings is None when any.
    ``accounts`` is the owner's shared accounts file (default: read it now, ``accounts.py``). With
    no ``[outbox]`` there is no problem (and no settings): this is also the plugin's
    ``Plugin.check``, which judges only the Outbox's own section, so removing it is allowed."""
    s = section(raw)
    if s is None:
        return None, []
    errors: list[str] = []
    if accounts is None:
        try:
            accounts = Accounts.load()
        except AccountsError as e:
            errors.append(f"the shared accounts file: {e}")
            accounts = Accounts()
    unknown = set(s) - TOP_KEYS
    if unknown:
        errors.append(f"[outbox] has unknown keys: {', '.join(sorted(unknown))}")
    owner, owner_from = _owner(s, raw, repo_root, accounts)
    if not isinstance(owner, str) or not _SOURCE.match(owner) or owner == MACHINE:
        errors.append(f"[outbox] owner must be a source name other than {MACHINE!r}")
    labels = s.get("merge_labels", [])
    if not isinstance(labels, list) or not all(isinstance(x, str) and x.strip() for x in labels):
        errors.append("[outbox] merge_labels must be a list of label names")
        labels = []
    if "people" in s and (not isinstance(s["people"], str) or not s["people"]):
        errors.append("[outbox] people must be a path to the identity map")
    host = s.get("host", "127.0.0.1")
    if not isinstance(host, str) or not _loopback(host):
        errors.append("[outbox] host must be a loopback address: the API is for machines on this computer")
    port = s.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        errors.append("[outbox] port must be 0..65535")
    systems: dict[str, System] = {}
    for name in SYSTEMS:
        try:
            if (sy := _system(s, name, accounts)) is not None:
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
    return Settings(owner, host, port, systems, kinds, owner_from=owner_from,
                    merge_labels=tuple(x.strip() for x in labels)), []


def settings(raw: dict[str, Any], repo_root: Path | None = None, accounts: Accounts | None = None) -> Settings:
    got, errors = problems(raw, repo_root, accounts)
    if errors:
        raise ConfigError("; ".join(errors))
    if got is None:
        raise ConfigError(NO_SECTION)
    return got


def valid_source(name: object) -> bool:
    return isinstance(name, str) and bool(_SOURCE.match(name))
