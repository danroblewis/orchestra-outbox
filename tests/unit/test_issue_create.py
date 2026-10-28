"""github.issue_create: an issue opened in a repository as the owner, always with the owner's approval."""

import tomllib

import pytest
from conftest import CANARY, approve, outbox_events, settle
from test_idempotent import force_due

from orchestra_outbox.client import OutboxError
from orchestra_outbox.config import problems
from orchestra_outbox.machine import Machine

KIND = '\n[outbox.kinds."upstream.issue"]\nsender = "github.issue_create"\n'


@pytest.fixture
def project(make_project):
    return make_project(KIND)


def test_a_drafted_issue_waits_for_the_owner_then_is_opened_once(project, machine, drafter, web, github):
    d = drafter.draft("upstream.issue", target={"repo": "acme/widgets"},
                      fields={"title": "gate: run_specs drops the last spec", "labels": "bug, orchestra"},
                      content="What broke, why, and the override's diff.")
    settle(machine)
    assert github.issues == []                                  # nothing before the owner approves
    approve(web, d["id"])
    settle(machine)
    assert len(github.issues) == 1
    issue = github.issues[0]
    assert issue["title"] == "gate: run_specs drops the last spec"
    assert issue["body"].startswith("What broke, why, and the override's diff.")
    assert f"orchestra-outbox:{d['id']}" in issue["body"]
    assert [x["name"] for x in issue["labels"]] == ["bug", "orchestra"]
    assert drafter.get(d["id"])["state"] == "sent"
    sent = [e for e in outbox_events(project) if e["type"] == "outbox.sent"]
    assert sent[0]["data"]["remote"] == {"repo": "acme/widgets", "number": issue["number"]}


def test_a_lost_answer_is_found_not_opened_again(project, machine, drafter, web, github):
    d = drafter.draft("upstream.issue", target={"repo": "acme/widgets"}, fields={"title": "t"}, content="body")
    approve(web, d["id"])
    github.lose_next = 1
    machine.step()
    assert len(github.issues) == 1 and drafter.get(d["id"])["state"] == "sending"
    force_due(machine)
    machine.step()
    assert len(github.issues) == 1 and drafter.get(d["id"])["state"] == "sent"


def test_a_draft_needs_a_title_and_a_repository(machine, drafter):
    with pytest.raises(OutboxError, match="needs the fields title"):
        drafter.draft("upstream.issue", target={"repo": "acme/widgets"}, content="body")
    with pytest.raises(OutboxError, match="target.repo must be owner/name"):
        drafter.draft("upstream.issue", target={"repo": "widgets"}, fields={"title": "t"}, content="body")


def test_auto_approval_is_refused_for_an_issue_kind():
    raw = tomllib.loads('[outbox]\n[outbox.github]\n[outbox.kinds."upstream.issue"]\nsender = "github.issue_create"\n'
                        'approval = "auto"\n')
    settings, errors = problems(raw)
    assert settings is None
    assert any('approval must be "always": github.issue_create' in e for e in errors)


def test_the_text_never_reaches_the_bus(project, web, github):
    m = Machine(project)
    m.announce()
    m.serve()
    try:
        from orchestra_outbox.client import OutboxClient

        c = OutboxClient.for_project(project, drafter="supervisor")
        d = c.draft("upstream.issue", target={"repo": "acme/widgets"}, fields={"title": CANARY, "labels": CANARY},
                    content=f"{CANARY} body")
        approve(web, d["id"])
        settle(m)
        m.flush()
    finally:
        m.server.stop()
    assert len(github.issues) == 1
    assert CANARY not in str(outbox_events(project))
