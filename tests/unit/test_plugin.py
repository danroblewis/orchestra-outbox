"""The plugin's declaration, Orchestra finding it, the web app serving it, and the measurements."""

import stat
import sys
from pathlib import Path

from conftest import approve, settle
from orchestra.config import Config, Project
from orchestra.plugins import load

from orchestra_outbox.plugin import plugin


def proj(toml: str) -> Project:
    return Project("k", Path("/repo"), Config.from_toml(toml))


def test_it_runs_only_where_it_is_configured():
    off = plugin(proj('[events]\nurl = "file"\n'))
    assert (off.name, off.processes, off.machines, off.collectors) == ("outbox", [], [], [])
    assert [p.path for p in off.pages] == ["/outbox"]
    on = plugin(proj("[outbox]\n"))
    [p] = on.processes
    assert (p.name, p.argv(), p.restart) == ("sender", [sys.executable, "-m", "orchestra_outbox", "run"], "always")
    assert [(m.id, m.kind, m.mode) for m in on.machines] == [("outbox", "program", "continuous")]
    [c] = on.collectors
    assert (c.name, c.kind, c.target) == ("outbox", "scrape", "http://127.0.0.1:9467/metrics")
    assert plugin(proj("[outbox]\nport = 0\n")).collectors == []


def test_orchestra_finds_the_installed_plugin():
    loaded = load(proj("[outbox]\n"))
    assert "outbox" in [p.name for p in loaded.plugins] and not loaded.errors


def test_the_web_app_serves_the_page_and_its_state(project, web):
    d = web.get("/api/plugins").json()
    assert {"plugin": "outbox", "path": "/outbox", "title": "Outbox"} in d["pages"] and d["errors"] == []
    page = web.get("/outbox").text
    assert "{{v}}" not in page and "/plugins/outbox/static/outbox.js" in page
    assert web.get("/plugins/outbox/static/outbox.js").status_code == 200
    s = web.get("/api/plugins/outbox/state").json()
    assert s["configured"] and s["owner"] == "person.owner" and s["waiting"] == [] and not s["running"]
    assert {k["name"]: k["approval"] for k in s["kinds"]}["sail.label"] == "auto"


def test_the_metrics_count_waiting_age_sent_and_rejected(project, machine, drafter, web):
    a = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="a")
    b = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="b")
    drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="c")
    approve(web, a["id"], content="a, edited")
    web.post(f"/api/plugins/outbox/drafts/{b['id']}/reject", json={"reason": "not_needed"})
    settle(machine)
    m = machine.metrics()
    assert 'outbox_drafts_waiting{kind="pr.comment"} 1' in m
    assert 'outbox_drafts_sent_total{kind="pr.comment"} 1' in m
    assert 'outbox_drafts_rejected_total{kind="pr.comment"} 1' in m
    assert 'outbox_drafts_approved_total{kind="pr.comment",by="owner",edited="true"} 1' in m
    assert 'outbox_drafts_waiting{kind="review.request"} 0' in m
    assert "outbox_oldest_draft_waiting_seconds " in m
    import urllib.request
    with urllib.request.urlopen(machine.server.url + "/metrics") as r:     # served without the token: no content
        assert 'outbox_drafts_waiting{kind="pr.comment"} 1' in r.read().decode()


def test_the_record_is_the_owners_alone(project, machine):
    db = project.state_dir / "outbox" / "outbox.db"
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700


def test_a_long_run_of_unedited_approvals_is_shown_with_a_suggestion_and_nothing_changes(project, machine, drafter, web):
    from orchestra_outbox.web import SUGGEST_AFTER

    for i in range(SUGGEST_AFTER):
        d = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content=f"c{i}")
        before = {k["name"]: k for k in web.get("/api/plugins/outbox/state").json()["kinds"]}["pr.comment"]
        assert before["unedited_streak"] == i and before["suggest"] is False
        approve(web, d["id"])
    s = web.get("/api/plugins/outbox/state").json()
    k = {k["name"]: k for k in s["kinds"]}["pr.comment"]
    assert (k["unedited_streak"], k["suggest"], k["approval"]) == (SUGGEST_AFTER, True, "always")    # never switched
    assert "ask the Supervisor" in (Path(__file__).parents[2] / "orchestra_outbox/static/outbox.js").read_text()
