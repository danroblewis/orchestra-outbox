"""The sender never exits on a configuration it does not like (the standing demo's crash of
2026-11-04 01:16Z). The Supervisor's ``outbox.kinds`` write left the private overlay with the
kinds alone, ``live.slack`` among them, and no ``[outbox.slack]``: ``Machine`` raised the check's
error and ``main`` returned 2, five times, until the launcher gave up and ``/metrics`` was gone.
Now what fails the check is reported (the check, the sender's log) with that kind off, and the
sender starts and serves its metrics with the rest."""

import logging
import subprocess
import urllib.request

import pytest
from conftest import opt_in
from orchestra.config import load_project

from orchestra_outbox.check import check
from orchestra_outbox.config import problems, standing
from orchestra_outbox.machine import Machine

# The project's orchestra.toml at the crash, its [outbox] as it stood (port 0 here, not 54252).
REPO_TOML = """[events]
url = "file"

[metrics]
url = "file"

[outbox]
port = 0
owner = "person.owner"

[outbox.github]
api = "https://api.github.com"
token_file = "/nonexistent/placeholder/github-token"

[outbox.kinds."live.pr_open"]
sender = "github.pr_open"
approval = "auto"

[outbox.kinds."live.sail"]
sender = "github.label"
approval = "auto"

[outbox.kinds."live.close_ticket"]
sender = "script"
command = ["/nonexistent/python", "/nonexistent/close_ticket.py", "--only-label", "orchestra-live-test"]
action = "other"
system = "jira"
template = "Done"
target = ["issue"]
timeout = "2m"
approval = "auto"

[outbox.kinds."live.create_ticket"]
sender = "script"
command = ["/nonexistent/python", "/nonexistent/create_ticket.py"]
action = "other"
system = "jira"
target = ["item"]
timeout = "2m"
approval = "auto"

[outbox.kinds."live.nudge"]
sender = "github.pr_comment"
approval = "always"
"""

# The private overlay the Supervisor's "outbox.kinds set" wrote (its other sections left out).
OVERLAY = """[outbox.kinds."live.pr_open"]
sender = "github.pr_open"
approval = "auto"

[outbox.kinds."live.sail"]
sender = "github.label"
approval = "auto"

[outbox.kinds."live.close_ticket"]
sender = "script"
command = ["/nonexistent/python", "/nonexistent/close_ticket.py", "--only-label", "orchestra-live-test"]
action = "other"
template = "Done"
target = ["issue"]
timeout = "2m"
approval = "auto"

[outbox.kinds."live.create_ticket"]
sender = "script"
command = ["/nonexistent/python", "/nonexistent/create_ticket.py"]
action = "other"
target = ["item"]
timeout = "2m"
approval = "auto"

[outbox.kinds."live.nudge"]
sender = "github.pr_comment"
approval = "always"

[outbox.kinds."live.slack"]
sender = "slack.message"
approval = "auto"

[outbox.kinds."supervisor.pull_request"]
sender = "github.pr_open"
approval = "auto"
"""

SLACK_MISSING = '[outbox.kinds."live.slack"] uses the sender slack.message, but [outbox.slack] is not configured'


@pytest.fixture
def live(tmp_path, monkeypatch, accounts_dir):
    """live(repo_toml, overlay): a project as the standing demo was, with W9's accounts file."""
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    for var in ("ORCHESTRA_EVENTS_URL", "ORCHESTRA_METRICS_URL", "ORCHESTRA_OUTBOX_URL", "ORCHESTRA_CONFIG_OVERLAY"):
        monkeypatch.delenv(var, raising=False)
    (accounts_dir / "accounts.toml").write_text('writes = ["github", "jira"]\n', encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    monkeypatch.setenv("ORCHESTRA_REPO", str(repo))

    def make(repo_toml: str = REPO_TOML, overlay: str | None = OVERLAY):
        (repo / "orchestra.toml").write_text(repo_toml)
        project = load_project(repo)
        if overlay is not None:
            project.overlay_file.parent.mkdir(parents=True, exist_ok=True)
            project.overlay_file.write_text(overlay)
            project = load_project(repo)
        return project
    return make


def test_the_crash_config_starts_the_sender_with_live_slack_off_and_serves_metrics(live, caplog):
    project = live()
    assert SLACK_MISSING in problems(project.config.raw, project.repo_root)[1]     # the check still refuses it
    with caplog.at_level(logging.WARNING, logger="orchestra_outbox"):
        m = Machine(project)                                 # raised ConfigError here; main() returned 2
    m.serve()
    try:
        with urllib.request.urlopen(m.server.url + "/metrics") as r:
            assert r.status == 200 and b"outbox_drafts_waiting" in r.read()
    finally:
        m.server.stop()
    assert "live.slack" not in m.settings.kinds and m.settings.off["live.slack"].startswith("live.slack is off: ")
    assert {"live.pr_open", "live.sail", "live.nudge", "supervisor.pull_request"} <= set(m.settings.kinds)
    assert SLACK_MISSING in caplog.text
    report = check(project.config.raw, project.repo_root)
    assert not report["ok"] and SLACK_MISSING in report["errors"] and "live.slack" in report["off"]


def test_a_draft_of_a_kind_that_does_not_pass_is_refused_and_the_others_draft(live):
    from orchestra_outbox.kinds import DraftError

    m = Machine(live())
    with pytest.raises(DraftError, match="live.slack is off"):
        m.outbox.request({"drafter": "worker/1", "kind": "live.slack", "target": {"channel": "C1"}, "content": "x"})
    d, new = m.outbox.request({"drafter": "worker/1", "kind": "live.nudge", "target": {"repo": "a/b", "number": 7},
                               "content": "a reminder"})
    assert new and d.state == "drafted"


@pytest.mark.parametrize("extra, top, named, off", [
    ('[outbox.kinds."x.legacy"]\nsender = "github.pr_comment"\nchannel = "C1"\n', "", "unknown keys", "x.legacy"),
    ('[outbox.kinds."x.who"]\nsender = "carrier.pigeon"\n', "", "sender must be one of", "x.who"),
    ('[outbox.kinds."x.script"]\nsender = "script"\ncommand = ["/bin/true"]\naction = "other"\n', "", None, "x.script"),
    ("", 'writes = ["github", "jira", "slack"]\n', "writes is refused", None),
])
def test_a_bad_key_is_reported_and_never_stops_the_sender(live, extra, top, named, off):
    toml = REPO_TOML.replace('owner = "person.owner"\n', 'owner = "person.owner"\n' + top) + "\n" + extra
    m = Machine(live(toml, overlay=None))
    assert "live.pr_open" in m.settings.kinds and m.metrics()
    if named:
        assert any(named in p for p in m.problems), m.problems
    if off:
        assert off in m.settings.off
    assert m.settings.writes == ("github", "jira")             # writes only ever from accounts.toml


def test_an_accounts_file_it_cannot_read_is_reported_and_writes_falls_back_to_github(live, accounts_dir):
    (accounts_dir / "accounts.toml").chmod(0o644)                 # readable by others: refused
    m = Machine(live(overlay=None))
    assert any("the shared accounts file" in p for p in m.problems)
    assert m.settings.writes == ("github",) and "live.close_ticket" in m.settings.off
    (accounts_dir / "accounts.toml").unlink()                    # missing: the default, no problem
    assert standing(m.project.config.raw, m.project.repo_root)[1] == []


def test_a_fix_after_a_bad_start_takes_effect_and_a_bad_edit_after_a_good_start_does_not(live, accounts_dir):
    project = live()
    m = Machine(project)
    assert "live.slack" in m.settings.off
    project.overlay_file.write_text(OVERLAY.replace('[outbox.kinds."live.slack"]\nsender = "slack.message"\n'
                                                    'approval = "auto"\n', ""))
    m._stamp = None
    assert m.reload() and m.problems == [] and m.settings.off == {}
    project.overlay_file.write_text(OVERLAY)                      # broken again: the last good one stands
    m._stamp = None
    assert not m.reload() and "live.slack" not in m.settings.off
    opt_in(accounts_dir)
