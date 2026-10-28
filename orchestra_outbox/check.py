"""``orchestra-outbox check``: validate an ``[outbox]`` configuration before it is committed.

For the Supervisor's tools (and anyone editing by hand)::

    orchestra-outbox check                      # orchestra.toml of the repository here
    orchestra-outbox check --repo ~/work        # ... of that repository
    orchestra-outbox check --file proposed.toml # a whole orchestra.toml, not yet in place
    orchestra-outbox check --file - --json      # read from stdin; answer in JSON
    orchestra-outbox example                    # the owner's five starting kinds, to install and check
    orchestra-outbox limits --json              # the rate limits in use, their backlog and usage

Exit 0 when the configuration is valid, 1 when it is not, 2 when it cannot be read. The JSON is
``{"ok": bool, "errors": [...], "warnings": [...], "owner": id, "owner_from": where,
"kinds": {name: {sender, approval, limit, per, ping, action}}}``.
Errors are what the outbox would refuse to start with. Warnings are what would make a send fail
later (a script that is not there or not executable, a token file with loose permissions); they do
not fail the check, because the target repository or the token may arrive after the config."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tomllib
from pathlib import Path

from . import actions as X
from .config import problems
from .senders.script import argv

EXAMPLE = Path(__file__).parent / "examples" / "owner-kinds.toml"


def warnings(s, repo_root: Path) -> list[str]:
    out: list[str] = []
    for k in s.kinds.values():
        if k.sender == "script":
            cmd = argv(k.command, repo_root)[0]
            if not os.path.isfile(cmd):
                out.append(f'[outbox.kinds."{k.name}"] command {k.command[0]!r} is not a file '
                           f"(looked at {cmd}); sends will fail until it is")
            elif not os.access(cmd, os.X_OK):
                out.append(f'[outbox.kinds."{k.name}"] command {cmd} is not executable')
            if X.script_approves(k.command, cmd):
                out.append(f'[outbox.kinds."{k.name}"] {cmd} names a way to approve a pull request: every send of it '
                           "will be refused")
        if k.sender == "github.label" and k.approval == "auto" and not s.merge_labels:
            out.append(f'[outbox.kinds."{k.name}"] adds labels without your approval, and [outbox] merge_labels is empty: '
                       "name the label that merges (SAIL) so its first use per pull request is yours")
    if s.owner_from == "default":
        out.append('[outbox] owner is not set: approvals publish as "owner". Set it to your person id '
                   '(owner = "person.<key>" here or in ~/.config/orchestra/accounts.toml), or list your accounts '
                   "there and in the identity map")
    elif not s.owner.startswith("person."):
        out.append(f"[outbox] owner {s.owner!r} is not a person id (person.<key>, as the identity map names people)")
    for name, sy in s.systems.items():
        if sy.token_file and sy.token_file.exists() and sy.token_file.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            out.append(f"[outbox.{name}] token_file {sy.token_file} is readable by others: chmod 600 it")
    return out


def check(raw: dict, repo_root: Path) -> dict:
    s, errors = problems(raw, repo_root)
    if s is None:
        return {"ok": False, "errors": errors, "warnings": [], "kinds": {}}
    return {"ok": True, "errors": [], "warnings": warnings(s, repo_root), "owner": s.owner, "owner_from": s.owner_from,
            "kinds": {k.name: {"sender": k.sender, "approval": k.approval, "limit": k.limit, "per": k.per,
                               "ping": k.ping, "action": k.action}
                      for k in s.kinds.values()}}


def limits(repo: Path) -> dict:
    """The rate limits of the project at ``repo``, read from its record (``machine.limits_view``)."""
    import time

    from orchestra.config import load_project

    from .config import settings
    from .machine import limits_view
    from .store import Store

    project = load_project(repo)
    return {"ok": True, **limits_view(Store.for_project(project), settings(project.config.raw, project.repo_root),
                                      time.time())}


def main(args: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestra-outbox", description="The Outbox's tools.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate [outbox] in an orchestra.toml")
    c.add_argument("--repo", type=Path, default=Path.cwd(), help="the repository (default: here)")
    c.add_argument("--file", help="an orchestra.toml to check instead of the repository's; - for stdin")
    c.add_argument("--json", action="store_true", help="answer in JSON")
    sub.add_parser("example", help="print the owner's five starting kinds (approval always)")
    lim = sub.add_parser("limits", help="the rate limits in use: limit, backlog, unresolvable, usage")
    lim.add_argument("--repo", type=Path, default=Path.cwd(), help="the repository (default: here)")
    lim.add_argument("--json", action="store_true", help="answer in JSON")
    a = ap.parse_args(args)
    if a.cmd == "example":
        print(EXAMPLE.read_text(encoding="utf-8"), end="")
        return 0
    if a.cmd == "limits":
        return _limits(a)
    try:
        if a.file == "-":
            text = sys.stdin.read()
        else:
            text = Path(a.file or a.repo / "orchestra.toml").read_text(encoding="utf-8")
        raw = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(json.dumps({"ok": False, "errors": [f"cannot read it: {e}"]}) if a.json else f"cannot read it: {e}")
        return 2
    out = check(raw, a.repo.resolve())
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        for e in out["errors"]:
            print(f"error: {e}")
        for w in out["warnings"]:
            print(f"warning: {w}")
        if out["ok"]:
            kinds = ", ".join(f"{n} ({v['sender']}, {v['approval']})" for n, v in out["kinds"].items()) or "none"
            print(f"ok: kinds {kinds}; approvals publish as {out['owner']} ({out['owner_from']})")
    return 0 if out["ok"] else 1


def _limits(a) -> int:
    from orchestra.config import ConfigError

    try:
        out = limits(a.repo.resolve())
    except (ConfigError, OSError, ValueError) as e:
        print(json.dumps({"ok": False, "error": str(e)}) if a.json else f"cannot read it: {e}")
        return 2
    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    if not out["limits"]:
        print("no rate limit is in use")
    for v in out["limits"]:
        what = f"{v['kind']} to {v['recipient']}" if v["scope"] == "kind" else f"the {v['system']} API ({v['api']})"
        cap = f"{v['limit']} per {v['per']:g}s" if v["limit"] is not None else "the API's"
        flag = f"UNRESOLVABLE ({', '.join(v['why'])})" if v["unresolvable"] else "ok"
        print(f"{flag}: {what}: limit {cap}; sent {v['sent_in_window']} in the window; {v['backlog']} waiting "
              f"(oldest {v['oldest_wait']:.0f}s) {v['backlog_by_drafter']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
