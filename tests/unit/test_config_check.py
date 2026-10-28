"""The configuration contract and ``orchestra outbox check``, which the Supervisor's tools call."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from orchestra.config import ConfigError

from orchestra_outbox.check import check, main
from orchestra_outbox.config import settings

GOOD = """
[outbox]
[outbox.slack]
[outbox.kinds."review.request"]
sender = "slack.message"
template = "RR: $link cc: $reviewers"
"""


def problems(toml: str) -> list[str]:
    return check(tomllib.loads(toml), Path("/nonexistent"))["errors"]


def test_a_good_configuration_passes_and_defaults_to_always():
    s = settings(tomllib.loads(GOOD))
    k = s.kinds["review.request"]
    assert (k.sender, k.approval, k.limit, s.owner, s.port) == ("slack.message", "always", None, "owner", 9467)
    assert s.systems["slack"].api == "https://slack.com/api"


@pytest.mark.parametrize("toml, reason", [
    (GOOD + 'bogus = 1\n', "unknown keys: bogus"),
    ('[outbox]\n[outbox.kinds."x.y"]\nsender = "slack.post"\n', "sender must be one of"),
    ('[outbox]\n[outbox.kinds."x.y"]\nsender = "slack.message"\n', "[outbox.slack] is not configured"),
    ('[outbox]\n[outbox.slack]\ntoken = "xoxp-1"\n', "token is refused"),
    ('[outbox]\n[outbox.slack]\napi = "https://evil.example/api"\n', "must be on slack.com"),
    ('[outbox]\n[outbox.jira]\n', "api must be a URL (Jira has no default"),
    ('[outbox]\n[outbox.github]\napi = "http://github.example"\n', "must be https"),
    ('[outbox]\n[outbox.slack]\n[outbox.kinds."a"]\nsender = "slack.message"\napproval = "sometimes"\n', 'approval must be'),
    ('[outbox]\n[outbox.slack]\n[outbox.kinds."a"]\nsender = "slack.message"\ntemplate = "hi $"\n', "malformed placeholder"),
    ('[outbox]\n[outbox.kinds."a"]\nsender = "script"\n', "needs command"),
    ('[outbox]\n[outbox.slack]\n[outbox.kinds."a"]\nsender = "slack.message"\ncommand = ["x"]\n', "only for the script"),
    ('[outbox]\n[outbox.jira]\napi = "https://j.example"\n[outbox.kinds."a"]\nsender = "jira.create"\n', "needs a payload"),
    (('[outbox]\n[outbox.jira]\napi = "https://j.example"\n[outbox.kinds."a"]\nsender = "jira.create"\n'
     '[outbox.kinds."a".payload]\nsummary = "$s"\n'), "payload lacks project, issuetype"),
    ('[outbox]\n[outbox.slack]\n[outbox.kinds."Bad Name"]\nsender = "slack.message"\n', "lowercase words"),
    ('[outbox]\n[outbox.slack]\n[outbox.kinds."a"]\nsender = "slack.message"\nper = "10s"\n', "per must be at least"),
    ('[outbox]\nhost = "0.0.0.0"\n', "loopback"),
    ('[outbox]\nowner = "outbox"\n', "owner must be"),
    ("[events]\n", "no [outbox] section"),
])
def test_an_invalid_kind_configuration_is_rejected_with_a_clear_reason(toml, reason):
    errors = problems(toml)
    assert any(reason in e for e in errors), errors
    with pytest.raises(ConfigError):
        settings(tomllib.loads(toml))


def test_every_problem_is_reported_at_once():
    toml = ('[outbox]\n[outbox.kinds."a"]\nsender = "nope"\n[outbox.kinds."b"]\nsender = "script"\n')
    assert len(problems(toml)) == 2


def test_the_cli_checks_a_proposed_file_from_stdin_in_json(tmp_path):
    bad = '[outbox]\n[outbox.kinds."a"]\nsender = "nope"\n'
    p = subprocess.run([sys.executable, "-m", "orchestra", "outbox", "check", "--file", "-", "--json",
                        "--repo", str(tmp_path)], input=bad, capture_output=True, text=True, check=False)
    assert p.returncode == 1
    out = json.loads(p.stdout)
    assert out["ok"] is False and "sender must be one of" in out["errors"][0]
    p = subprocess.run([sys.executable, "-m", "orchestra", "outbox", "check", "--file", "-", "--repo", str(tmp_path)],
                       input=GOOD, capture_output=True, text=True, check=False)
    assert p.returncode == 0 and "ok: kinds review.request (slack.message, always)" in p.stdout


def test_check_warns_of_a_missing_script_but_passes(tmp_path, capsys):
    (tmp_path / "orchestra.toml").write_text('[outbox]\n[outbox.kinds."port"]\nsender = "script"\n'
                                             'command = "scripts/port.sh --to release"\naction = "other"\n')
    assert main(["check", "--repo", str(tmp_path), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and "is not a file" in out["warnings"][0]
    assert settings(tomllib.loads((tmp_path / "orchestra.toml").read_text())).kinds["port"].command == (
        "scripts/port.sh", "--to", "release")


def test_the_machine_takes_a_new_kind_without_a_restart_and_keeps_the_last_good_config(project, machine, drafter):
    import os
    import time

    from orchestra_outbox.client import OutboxError

    toml = project.repo_root / "orchestra.toml"
    text = toml.read_text()
    toml.write_text(text + '\n[outbox.kinds."ping"]\nsender = "slack.message"\n')
    os.utime(toml, (time.time() + 5, time.time() + 5))
    machine.step()
    assert drafter.draft("ping", target={"channel": "C0REVIEW"}, content="hi")["state"] == "drafted"
    toml.write_text(text + '\n[outbox.kinds."ping"]\nsender = "nope"\n')
    os.utime(toml, (time.time() + 10, time.time() + 10))
    machine.step()
    assert "ping" in machine.settings.kinds                       # the bad edit did not take
    toml.write_text(text)
    os.utime(toml, (time.time() + 15, time.time() + 15))
    machine.step()
    with pytest.raises(OutboxError):
        drafter.draft("ping", target={"channel": "C0REVIEW"}, content="hi")


def test_the_specs_example_configuration_passes_the_check():
    spec = (Path(__file__).parents[2] / "SPEC.md").read_text()
    block = spec.split("### 4.4 Examples", 1)[1].split("```toml", 1)[1].split("```", 1)[0]
    out = check(tomllib.loads(block), Path("/nonexistent"))
    assert out["ok"], out["errors"]
    assert out["kinds"]["review.ping"] == {"sender": "slack.reply", "approval": "auto", "limit": 1, "per": 86400.0,
                                           "ping": True, "action": "slack.post"}
