"""Read-only by default: ``writes`` in the owner's accounts.toml (default ``["github"]``) lists the
systems the Outbox may write to. A kind of any other system is off at the check (reported, never an
error) and refused at drafting and at sending, never sent. ``writes`` is refused in orchestra.toml."""

import json
import tomllib
from pathlib import Path

from conftest import approve, opt_in, settle

from orchestra_outbox.check import check, main
from orchestra_outbox.config import problems
from orchestra_outbox.machine import Machine

SCRIPT_KIND = '\n[outbox.kinds."close"]\nsender = "script"\ncommand = ["scripts/close.sh"]\naction = "other"\n'


def default(accounts_dir) -> None:
    (accounts_dir / "accounts.toml").unlink()            # no accounts.toml: the default applies


def draft(m: Machine, kind: str, target: dict, content: str = "hello") -> dict:
    d, _ = m.outbox.request({"drafter": "worker/1", "kind": kind, "target": target, "content": content,
                             "fields": {"link": "x", "reviewers": "y"}})
    return d.view()


def test_the_default_refuses_a_jira_comment_and_a_slack_post_and_sends_a_pr_comment(make_project, accounts_dir,
                                                                                     web, slack, github, jira):
    default(accounts_dir)
    m = Machine(make_project())
    key = jira.issue()
    j = draft(m, "jira.comment", {"issue": key})
    s = draft(m, "review.request", {"channel": "C0TEAM"})
    g = draft(m, "pr.comment", {"repo": "acme/widgets", "number": 7}, "Looks fine to me.")
    assert (j["state"], j["error"]) == ("failed", "refused:read_only:jira")
    assert (s["state"], s["error"]) == ("failed", "refused:read_only:slack")
    assert g["state"] == "drafted"
    approve(web, g["id"])
    settle(m)
    assert m.store.get(g["id"]).state == "sent"
    assert [c["body"].startswith("Looks fine to me.") for c in github.comments_on(7)] == [True]
    assert jira.issues[key]["comments"] == [] and slack.posted == []


def test_a_draft_approved_before_the_owner_turns_jira_off_is_refused_at_sending(make_project, accounts_dir, web, jira):
    m = Machine(make_project())
    key = jira.issue()
    d = draft(m, "jira.comment", {"issue": key})
    approve(web, d["id"])
    default(accounts_dir)                                # the owner takes jira out while it waits
    settle(m)
    got = m.store.get(d["id"])
    assert (got.state, got.error) == ("failed", "refused:read_only:jira")
    assert jira.issues[key]["comments"] == []


def test_writes_github_and_jira_allows_the_comment(make_project, accounts_dir, web, slack, jira):
    (accounts_dir / "accounts.toml").write_text('writes = ["github", "jira"]\n')
    m = Machine(make_project())
    key = jira.issue()
    d = draft(m, "jira.comment", {"issue": key}, "Deployed.")
    assert draft(m, "review.request", {"channel": "C0TEAM"})["error"] == "refused:read_only:slack"
    approve(web, d["id"])
    settle(m)
    assert [c["body"] for c in jira.issues[key]["comments"]] == ["Deployed."]


def _script(project) -> Path:
    (project.repo_root / "scripts").mkdir()
    s = project.repo_root / "scripts" / "close.sh"
    s.write_text('#!/bin/sh\ntouch "$OUT_MARK"\n')
    s.chmod(0o755)
    return s


def test_a_script_kind_that_names_no_system_is_refused_even_when_every_system_is_on(make_project, web, tmp_path,
                                                                                     monkeypatch):
    mark = tmp_path / "ran"
    monkeypatch.setenv("OUT_MARK", str(mark))
    project = make_project(SCRIPT_KIND)
    _script(project)
    out = check(project.config.raw, project.repo_root)
    assert out["ok"] and "must say which system it writes to" in out["off"]["close"]
    m = Machine(project)
    d = draft(m, "close", {"issue": "OPS-1"})
    assert (d["state"], d["error"]) == ("failed", "refused:read_only:undeclared")
    settle(m)
    assert not mark.exists()


def test_a_script_kind_with_system_jira_follows_the_list(make_project, accounts_dir, web, tmp_path, monkeypatch):
    mark = tmp_path / "ran"
    monkeypatch.setenv("OUT_MARK", str(mark))
    default(accounts_dir)
    project = make_project(SCRIPT_KIND + 'system = "jira"\n')
    _script(project)
    m = Machine(project)
    assert draft(m, "close", {"issue": "OPS-1"})["error"] == "refused:read_only:jira"
    assert not mark.exists()
    opt_in(accounts_dir)                                 # the owner lists jira
    m = Machine(project)
    d = draft(m, "close", {"issue": "OPS-1"})
    approve(web, d["id"])
    settle(m)
    assert m.store.get(d["id"]).state == "sent" and mark.exists()


def test_writes_in_orchestra_toml_is_refused_at_the_check():
    for toml in ('[outbox]\nwrites = ["jira"]\n', '[outbox]\n[outbox.jira]\napi = "https://j.example.com"\nwrites = true\n',
                 'writes = ["slack"]\n[outbox]\n'):
        _, errors = problems(tomllib.loads(toml))
        assert any("writes is refused" in e and "accounts.toml" in e for e in errors), (toml, errors)


def test_the_check_names_the_off_kinds_and_why(make_project, accounts_dir, capsys):
    default(accounts_dir)
    project = make_project()
    out = check(project.config.raw, project.repo_root)
    assert out["ok"] and not out["errors"] and out["writes"] == ["github"] and out["writes_from"] == "default"
    assert set(out["off"]) == {"review.request", "slack.reply", "jira.comment", "jira.done", "jira.task"}
    assert "writes to jira, which is read-only" in out["off"]["jira.comment"]
    assert "only the owner can turn a system on" in out["off"]["review.request"]
    assert out["kinds"]["pr.comment"]["off"] is None and out["kinds"]["jira.done"]["system"] == "jira"
    assert main(["check", "--repo", str(project.repo_root)]) == 0
    text = capsys.readouterr().out
    assert "off: jira.comment is off: it writes to jira" in text and "ok: kinds" in text
    assert main(["check", "--repo", str(project.repo_root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["off"].keys() == out["off"].keys()


def test_writes_in_accounts_toml_must_be_a_list_of_names(accounts_dir):
    (accounts_dir / "accounts.toml").write_text('writes = "jira"\n')
    _, errors = problems(tomllib.loads("[outbox]\n"))
    assert any("writes must be a list" in e for e in errors)
