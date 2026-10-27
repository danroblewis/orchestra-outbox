"""``orchestra-outbox check``: validate an ``[outbox]`` configuration before it is committed.

For the Supervisor's tools (and anyone editing by hand)::

    orchestra-outbox check                      # orchestra.toml of the repository here
    orchestra-outbox check --repo ~/work        # ... of that repository
    orchestra-outbox check --file proposed.toml # a whole orchestra.toml, not yet in place
    orchestra-outbox check --file - --json      # read from stdin; answer in JSON

Exit 0 when the configuration is valid, 1 when it is not, 2 when it cannot be read. The JSON is
``{"ok": bool, "errors": [...], "warnings": [...], "kinds": {name: {sender, approval, limit, per}}}``.
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

from .config import problems
from .senders.script import argv


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
    for name, sy in s.systems.items():
        if sy.token_file and sy.token_file.exists() and sy.token_file.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            out.append(f"[outbox.{name}] token_file {sy.token_file} is readable by others: chmod 600 it")
    return out


def check(raw: dict, repo_root: Path) -> dict:
    s, errors = problems(raw)
    if s is None:
        return {"ok": False, "errors": errors, "warnings": [], "kinds": {}}
    return {"ok": True, "errors": [], "warnings": warnings(s, repo_root),
            "kinds": {k.name: {"sender": k.sender, "approval": k.approval, "limit": k.limit, "per": k.per}
                      for k in s.kinds.values()}}


def main(args: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestra-outbox", description="The Outbox's tools.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate [outbox] in an orchestra.toml")
    c.add_argument("--repo", type=Path, default=Path.cwd(), help="the repository (default: here)")
    c.add_argument("--file", help="an orchestra.toml to check instead of the repository's; - for stdin")
    c.add_argument("--json", action="store_true", help="answer in JSON")
    a = ap.parse_args(args)
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
            print(f"ok: kinds {kinds}")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
