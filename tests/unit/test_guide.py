"""The operator's guide (``orchestra_outbox/guide.md``) ships in the package, and every TOML example
in it is one the plugin's own check accepts: no errors and no warnings."""

import os
import re
import tomllib
from pathlib import Path

import pytest

import orchestra_outbox
from orchestra_outbox.accounts import Accounts
from orchestra_outbox.check import check

GUIDE = Path(orchestra_outbox.__file__).parent / "guide.md"
BLOCKS = re.findall(r"^```toml\n(.*?)^```", GUIDE.read_text(encoding="utf-8"), re.DOTALL | re.MULTILINE)
# What a project has around a fragment: the three systems, and an owner (accounts.toml gives one).
BASE = {"outbox": {"slack": {}, "github": {}, "jira": {"api": "https://jira.example.com"}}}


def merged(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = merged(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def test_the_guide_is_in_the_package():
    assert GUIDE.is_file() and GUIDE.stat().st_size > 0
    assert BLOCKS, "the guide has no ```toml examples"


@pytest.mark.parametrize("i", range(len(BLOCKS)))
def test_every_toml_example_parses_and_passes_the_check(i, tmp_path, monkeypatch):
    raw = tomllib.loads(BLOCKS[i])
    if "outbox" not in raw:                                    # the shared accounts.toml
        d = tmp_path / "guide-accounts"
        d.mkdir(mode=0o700)
        (d / "accounts.toml").write_text(BLOCKS[i], encoding="utf-8")
        os.chmod(d / "accounts.toml", 0o600)
        a = Accounts.load(d)
        assert a.owner and all(a.account(s) for s in ("slack", "github", "jira")), BLOCKS[i]
        return
    repo = tmp_path / "repo"
    for k in raw["outbox"].get("kinds", {}).values():          # a script example's file, in place
        if k.get("sender") == "script" and "/" in k["command"][0]:
            script = repo / k["command"][0]
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
    monkeypatch.setattr("orchestra_outbox.config.Accounts.load", lambda *a: Accounts(owner="person.test"))
    out = check(merged(BASE, raw), repo)
    assert out["ok"] and not out["errors"] and not out["warnings"], (BLOCKS[i], out)
