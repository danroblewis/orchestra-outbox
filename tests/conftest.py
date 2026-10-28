"""What the tests share: the three fakes, a project in a fresh git repository under a temporary
ORCHESTRATOR_HOME with the outbox configured against them, the outbox machine in-process, and the
web app with the plugin mounted."""

import subprocess

import pytest
from fake_github import FakeGitHub
from fake_jira import FakeJira
from fake_slack import FakeSlack
from orchestra.config import load_project
from orchestra.events import Events

CANARY = "CANARY-0b7e-never-on-the-bus"

BASE_TOML = """[events]
url = "file"

[metrics]
url = "file"

[outbox]
owner = "person.owner"
port = {port}

[outbox.slack]
api = "{slack}/api"

[outbox.github]
api = "{github}"

[outbox.jira]
api = "{jira}"

[outbox.kinds."review.request"]
sender = "slack.message"
template = "RR: $link cc: $reviewers"

[outbox.kinds."slack.reply"]
sender = "slack.reply"

[outbox.kinds."pr.comment"]
sender = "github.pr_comment"

[outbox.kinds."sail.label"]
sender = "github.label"
approval = "auto"
limit = 1
per = "24h"

[outbox.kinds."jira.comment"]
sender = "jira.comment"

[outbox.kinds."jira.done"]
sender = "jira.transition"
template = "Done"

[outbox.kinds."jira.task"]
sender = "jira.create"
[outbox.kinds."jira.task".payload]
project = {{ key = "OPS" }}
issuetype = {{ name = "Task" }}
summary = "$summary"
description = "$content"
labels = ["orchestra"]
"""


@pytest.fixture
def slack():
    with FakeSlack() as f:
        yield f


@pytest.fixture
def github():
    with FakeGitHub() as f:
        f.pr(7)
        yield f


@pytest.fixture
def jira():
    with FakeJira() as f:
        yield f


@pytest.fixture
def make_project(tmp_path, monkeypatch, slack, github, jira):
    """make_project(extra_toml="", port=0): a project configured against the fakes."""
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    monkeypatch.delenv("ORCHESTRA_METRICS_URL", raising=False)
    monkeypatch.delenv("ORCHESTRA_OUTBOX_URL", raising=False)
    monkeypatch.setenv("SLACK_OUTBOX_TOKEN", slack.TOKEN)
    monkeypatch.setenv("GITHUB_OUTBOX_TOKEN", github.TOKEN)
    monkeypatch.setenv("JIRA_OUTBOX_TOKEN", jira.TOKEN)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    monkeypatch.setenv("ORCHESTRA_REPO", str(repo))

    def make(extra: str = "", port: int = 0):
        (repo / "orchestra.toml").write_text(BASE_TOML.format(port=port, slack=slack.base, github=github.base,
                                                              jira=jira.base) + extra)
        return load_project(repo)
    return make


@pytest.fixture
def project(make_project):
    return make_project()


@pytest.fixture
def machine(project):
    """The outbox machine in-process, serving its API (not yet stepping: call ``.step()``)."""
    from orchestra_outbox.machine import Machine

    m = Machine(project)
    m.announce()
    m.serve()
    yield m
    m.server.stop()


@pytest.fixture
def web(project):
    from fastapi.testclient import TestClient
    from orchestra.web.app import create_app
    from orchestra.web.auth import bearer

    return TestClient(create_app(project), headers=bearer(project))


@pytest.fixture
def drafter(project, machine):
    from orchestra_outbox.client import OutboxClient

    return OutboxClient.for_project(project, drafter="worker/1")


def bus(project) -> list[dict]:
    return Events.for_project(project).after(0)[0]


def outbox_events(project) -> list[dict]:
    return [e for e in bus(project) if e["type"].startswith("outbox.")]


def approve(web, draft_id, content=None):
    r = web.post(f"/api/plugins/outbox/drafts/{draft_id}/approve", json={} if content is None else {"content": content})
    assert r.status_code == 200, r.text
    return r.json()["draft"]


def reject(web, draft_id, reason="content", note=None):
    r = web.post(f"/api/plugins/outbox/drafts/{draft_id}/reject", json={"reason": reason, "note": note})
    assert r.status_code == 200, r.text
    return r.json()["draft"]


def settle(machine, rounds: int = 3) -> None:
    for _ in range(rounds):
        machine.step()

