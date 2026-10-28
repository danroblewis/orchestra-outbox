"""The owner's decisions of 2026-10-27 on who a draft reaches and what it may do: a ping names its
recipients; opening a pull request is a kind like any other; approving one is refused everywhere;
the first merge action on each pull request is the owner's."""

import tomllib
from pathlib import Path

import pytest
from conftest import approve, outbox_events, settle
from fake_slack import REVIEW

from orchestra_outbox.check import check
from orchestra_outbox.kinds import DraftError
from orchestra_outbox.machine import Machine
from orchestra_outbox.senders import SendError

EXTRA = '''
[outbox.kinds."nudge"]
sender = "slack.message"
ping = true

[outbox.kinds."pr.label"]
sender = "github.label"
approval = "auto"

[outbox.kinds."pr.open"]
sender = "github.pr_open"
'''


@pytest.fixture
def project(make_project):
    return make_project(EXTRA, top='merge_labels = ["SAIL"]\n')


@pytest.fixture
def m(project):
    return Machine(project)


def req(m, kind, **body):
    return m.outbox.request({"drafter": "worker/1", "kind": kind, **body})[0]


def test_a_ping_without_named_recipients_is_refused_and_other_kinds_default_them(m):
    with pytest.raises(DraftError, match="nudge is a ping: name the people it reaches in recipients"):
        req(m, "nudge", target={"channel": REVIEW}, content="hi")
    assert req(m, "nudge", target={"channel": REVIEW}, content="hi", recipients=["U0ANA"]).recipients == ["U0ANA"]
    assert req(m, "pr.comment", target={"repo": "acme/widgets", "number": 7}, content="x").recipients == [
        "acme/widgets#7"]


def test_a_kind_or_script_that_approves_a_pull_request_is_refused_by_the_check():
    base = '[outbox]\n[outbox.github]\n[outbox.kinds."a"]\nsender = "script"\ncommand = {cmd}\naction = "{action}"\n'
    cases = [('["scripts/x.sh"]', "github.pr_approve"),
             ('"gh pr review 7 --approve"', "other"),
             ('["gh", "pr", "review", "7", "-a"]', "other"),
             ('"gh api -X POST repos/a/b/pulls/7/reviews -f event=APPROVE"', "other")]
    for cmd, action in cases:
        errors = check(tomllib.loads(base.format(cmd=cmd, action=action)), Path("/nonexistent"))["errors"]
        assert any("approves a pull request" in e for e in errors), (cmd, errors)
    ok = check(tomllib.loads(base.format(cmd='"gh pr review 7 --comment -b hi"', action="other")), Path("/x"))
    assert ok["ok"], ok
    errors = check(tomllib.loads('[outbox]\n[outbox.kinds."a"]\nsender = "script"\ncommand = "x"\n'), Path("/x"))["errors"]
    assert any("needs action" in e for e in errors)


def test_a_script_file_that_approves_is_warned_at_the_check_and_refused_at_sending(make_project, web):
    project = make_project('\n[outbox.kinds."sneaky"]\nsender = "script"\ncommand = ["scripts/s.sh"]\naction = "other"\n')
    (project.repo_root / "scripts").mkdir()
    s = project.repo_root / "scripts" / "s.sh"
    s.write_text('#!/bin/sh\ncurl -X POST "$API/repos/a/b/pulls/7/reviews" -d \'{"event": "APPROVE"}\'\n'
                 'touch "$OUT_MARK"\n')
    s.chmod(0o755)
    out = check(project.config.raw, project.repo_root)
    assert out["ok"] and any("approve a pull request" in w for w in out["warnings"])
    m = Machine(project)
    d = req(m, "sneaky", content="x")
    approve(web, d.id)
    settle(m)
    got = m.store.get(d.id)
    assert got.state == "failed" and got.error == "refused:pr_approval" and got.attempts == 1


def test_approval_content_is_refused_at_drafting_at_the_owners_edit_and_by_the_sender(m, web, github):
    pr = {"repo": "acme/widgets", "number": 7}
    with pytest.raises(DraftError, match="approves a pull request"):
        req(m, "pr.label", target=pr, content="lgtm")
    with pytest.raises(DraftError, match="approves a pull request"):
        req(m, "pr.comment", target=pr, content="Looks fine.\n/approve")
    d = req(m, "pr.comment", target=pr, content="Looks fine.")
    r = web.post(f"/api/plugins/outbox/drafts/{d.id}/approve", json={"content": "Looks fine.\n/lgtm"})
    assert r.status_code == 400 and "approves a pull request" in r.json()["detail"]
    sender = m.senders["github"]
    for path, body in (("repos/acme/widgets/pulls/7/reviews", {"event": "APPROVE"}),
                       ("repos/acme/widgets/pulls/7/reviews", {}),
                       ("repos/acme/widgets/pulls/7/reviews/9/events", {"event": "APPROVE"})):
        with pytest.raises(SendError, match="refused:pr_approval"):
            sender.call("POST", path, body=body)
    assert github.reviews == []                                   # nothing reached GitHub


def test_the_first_merge_action_on_a_pull_request_is_the_owners_and_later_ones_follow_the_kind(m, web, github):
    github.pr(8)
    pr7, pr8 = {"repo": "acme/widgets", "number": 7}, {"repo": "acme/widgets", "number": 8}
    first = req(m, "pr.label", target=pr7, content="SAIL")
    assert (first.state, first.held) == ("drafted", "first_merge")              # auto, but the first is the owner's
    other = req(m, "pr.label", target=pr7, content="needs-review")
    assert other.state == "approved"                                             # not a merge action
    settle(m)
    assert github.labels[7] == ["needs-review"]
    approve(web, first.id)
    settle(m)
    assert github.labels[7] == ["needs-review", "SAIL"]
    github.labels[7].remove("SAIL")                                              # removed, as a bot or a person might
    second = req(m, "pr.label", target=pr7, content="sail")
    assert (second.state, second.approved_by) == ("approved", "policy")          # later ones follow the kind
    elsewhere = req(m, "pr.label", target=pr8, content="SAIL")
    assert elsewhere.held == "first_merge"                                       # per pull request
    m.flush()
    held = [e for e in outbox_events(m.project) if e["type"] == "outbox.draft.held"]
    assert [(e["data"]["reason"], e["data"]["pr"]) for e in held] == [("first_merge", "acme/widgets#7"),
                                                                      ("first_merge", "acme/widgets#8")]


def test_a_merge_script_needs_the_pull_request_and_its_first_use_is_the_owners(make_project):
    project = make_project('\n[outbox.kinds."merge"]\nsender = "script"\ncommand = ["scripts/merge.sh"]\n'
                             'action = "github.pr_merge"\napproval = "auto"\n')
    m = Machine(project)
    with pytest.raises(DraftError, match="needs target keys repo, number"):
        req(m, "merge", content="merge it")
    d = req(m, "merge", target={"repo": "acme/widgets", "number": 7}, content="merge it")
    assert d.held == "first_merge"


def test_opening_a_pull_request_is_a_kind_approved_by_the_owner_and_never_opened_twice(m, web, github):
    with pytest.raises(DraftError, match="needs the fields title"):
        req(m, "pr.open", target={"repo": "acme/widgets", "head": "fix-1", "base": "main"}, content="Body")
    d = req(m, "pr.open", target={"repo": "acme/widgets", "head": "fix-1", "base": "main"}, content="Fixes the bug.",
            fields={"title": "Fix the bug"})
    assert d.state == "drafted"                                                  # approval always, by default
    approve(web, d.id)
    github.lose_next = 1
    m.step()
    with m.store.tx() as c:
        c.execute("UPDATE drafts SET next_attempt_at = NULL")
    m.step()
    assert len(github.pulls) == 1 and github.pulls[0]["title"] == "Fix the bug"
    got = m.store.get(d.id)
    assert got.state == "sent" and got.remote == {"repo": "acme/widgets", "number": github.pulls[0]["number"]}
