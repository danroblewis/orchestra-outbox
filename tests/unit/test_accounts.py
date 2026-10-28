"""The owner's shared accounts (tokens, API addresses, account ids, outside every repository), the
owner's person id on approvals, and the five kinds the owner starts with."""

import os
import tomllib
from pathlib import Path

import pytest
from conftest import approve, outbox_events, settle
from fake_slack import REVIEW

from orchestra_outbox import accounts, credentials
from orchestra_outbox.check import EXAMPLE, check, main
from orchestra_outbox.machine import Machine


def private(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text)
    os.chmod(path, 0o600)
    return path


def test_a_token_comes_from_the_shared_directory_after_the_environment(accounts_dir, monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.delenv("SLACK_OUTBOX_TOKEN", raising=False)
    with pytest.raises(credentials.CredentialError, match="no slack token"):
        credentials.token("slack", repo, None, "https://slack.com/api")
    private(accounts_dir / "tokens" / "slack-token", "xoxp-shared\n")
    assert credentials.token("slack", repo, None, "https://slack.com/api") == "xoxp-shared"
    monkeypatch.setenv("SLACK_OUTBOX_TOKEN", "xoxp-env")
    assert credentials.token("slack", repo, None, "https://slack.com/api") == "xoxp-env"
    monkeypatch.delenv("SLACK_OUTBOX_TOKEN")
    os.chmod(accounts_dir / "tokens" / "slack-token", 0o644)
    with pytest.raises(credentials.CredentialError, match="readable by others"):
        credentials.token("slack", repo, None, "https://slack.com/api")
    inside = private(repo / "token", "x")
    with pytest.raises(credentials.CredentialError, match="inside the repository"):
        credentials.token("slack", repo, inside, "https://slack.com/api")


def test_the_phase_one_location_is_still_read(accounts_dir, monkeypatch, tmp_path):
    monkeypatch.delenv("JIRA_OUTBOX_TOKEN", raising=False)
    private(Path(os.environ["HOME"]) / ".config" / "orchestra-outbox" / "jira-token", "pat-legacy")
    (tmp_path / "repo").mkdir()
    assert credentials.token("jira", tmp_path / "repo", None, "https://jira.example.com") == "pat-legacy"


def test_a_sender_sends_with_the_shared_token(make_project, accounts_dir, monkeypatch, web, slack):
    monkeypatch.delenv("SLACK_OUTBOX_TOKEN")
    private(accounts_dir / "tokens" / "slack-token", slack.TOKEN)
    m = Machine(make_project())
    d = m.outbox.request({"drafter": "w", "kind": "slack.reply", "content": "On it.",
                          "target": {"channel": REVIEW, "thread_ts": slack.say(REVIEW, "can someone look?")}})[0]
    approve(web, d.id)
    settle(m)
    assert m.store.get(d.id).state == "sent" and slack.requests[-1][4] == f"Bearer {slack.TOKEN}"


def test_the_api_address_and_the_owner_come_from_accounts_toml(accounts_dir):
    raw = tomllib.loads('[outbox]\n[outbox.jira]\n[outbox.kinds."c"]\nsender = "jira.comment"\n')
    assert any("Jira has no default" in e for e in check(raw, Path("/x"))["errors"])
    private(accounts_dir / "accounts.toml", 'owner = "person.daniel"\n[jira]\napi = "https://jira.example.com"\n'
                                            'account = "dlewis"\n')
    out = check(raw, Path("/x"))
    assert out["ok"] and (out["owner"], out["owner_from"]) == ("person.daniel", "accounts")
    os.chmod(accounts_dir / "accounts.toml", 0o644)
    assert any("readable by others" in e for e in check(raw, Path("/x"))["errors"])


def test_the_owner_is_found_in_the_identity_map_by_their_accounts(accounts_dir, tmp_path):
    private(accounts_dir / "accounts.toml", '[github]\naccount = "DanRobLewis"\n[slack]\naccount = "U012AB3CD"\n')
    (tmp_path / "people.toml").write_text('[people.daniel]\nname = "Daniel Lewis"\ngithub = "danroblewis"\n'
                                          '[people.ana]\ngithub = "ana"\n[people.bot]\nbot = true\ngithub = "danroblewis"\n')
    raw = tomllib.loads('[bridges]\npeople = "people.toml"\n[outbox]\n')
    out = check(raw, tmp_path)
    assert (out["owner"], out["owner_from"]) == ("person.daniel", "identity map")
    explicit = check(tomllib.loads('[outbox]\nowner = "person.dan"\n'), tmp_path)
    assert (explicit["owner"], explicit["owner_from"]) == ("person.dan", "config")
    nobody = check(tomllib.loads("[outbox]\n"), Path("/x"))
    assert nobody["owner"] == "owner" and any("owner is not set" in w for w in nobody["warnings"])


def test_approvals_and_rejections_carry_the_owners_person_id(make_project, accounts_dir, web, slack):
    project = make_project()
    toml = project.repo_root / "orchestra.toml"
    toml.write_text(toml.read_text().replace('owner = "person.owner"\n', ""))
    private(accounts_dir / "accounts.toml", '[slack]\naccount = "U012AB3CD"\n')
    (project.repo_root / "people.toml").write_text('[people.daniel]\nslack = "U012AB3CD"\n')
    toml.write_text(toml.read_text().replace("[outbox]\n", '[outbox]\npeople = "people.toml"\n', 1))
    project.reload_config()
    m = Machine(project)
    assert m.settings.owner == "person.daniel"
    a = m.outbox.request({"drafter": "w", "kind": "pr.comment", "target": {"repo": "acme/widgets", "number": 7},
                          "content": "a"})[0]
    b = m.outbox.request({"drafter": "w", "kind": "pr.comment", "target": {"repo": "acme/widgets", "number": 7},
                          "content": "b"})[0]
    approve(web, a.id)
    web.post(f"/api/plugins/outbox/drafts/{b.id}/reject", json={"reason": "timing"})
    m.flush()
    assert m.store.get(a.id).approver == m.store.get(b.id).approver == "person.daniel"
    [ap] = [e for e in outbox_events(project) if e["type"] == "outbox.draft.approved"]
    [rj] = [e for e in outbox_events(project) if e["type"] == "outbox.draft.rejected"]
    assert ap["source"] == rj["source"] == ap["data"]["approver"] == "person.daniel"
    assert web.get("/api/plugins/outbox/state").json()["owner_from"] == "identity map"


def test_the_five_starting_kinds_pass_the_check_all_approved_by_the_owner(accounts_dir, tmp_path, capsys):
    private(accounts_dir / "accounts.toml", 'owner = "person.daniel"\n[jira]\napi = "https://jira.example.com"\n')
    out = check(tomllib.loads(EXAMPLE.read_text()), tmp_path)
    assert out["ok"], out["errors"]
    assert sorted(out["kinds"]) == ["jira.comment", "jira.create", "jira.transition", "review.requested",
                                    "slack.thread_reply"]
    assert {v["approval"] for v in out["kinds"].values()} == {"always"}
    assert out["kinds"]["review.requested"]["ping"] is True and out["warnings"] == []
    assert main(["example"]) == 0 and capsys.readouterr().out == EXAMPLE.read_text()
    (tmp_path / "proposed.toml").write_text(EXAMPLE.read_text())
    assert main(["check", "--file", str(tmp_path / "proposed.toml"), "--repo", str(tmp_path)]) == 0


def test_the_shared_module_stands_alone():
    """accounts.py imports nothing of the outbox's, so a common library can take it as it is."""
    src = Path(accounts.__file__).read_text()
    assert "from ." not in src and "orchestra_outbox" not in src and "import orchestra" not in src
