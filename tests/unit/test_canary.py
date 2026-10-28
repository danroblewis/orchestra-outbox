"""No draft's text, edit, note or script output reaches the bus, the metrics, a log or any file but
the outbox's own record."""

import json
import logging
from pathlib import Path

from conftest import CANARY, approve, bus, reject, settle
from fake_slack import REVIEW

from orchestra_outbox.machine import Machine


def test_no_content_on_the_bus_in_the_metrics_or_the_logs(make_project, tmp_path, monkeypatch, caplog, web, slack,
                                                           github, jira):
    caplog.set_level(logging.DEBUG)
    project = make_project('\n[outbox.kinds."echo"]\nsender = "script"\ncommand = ["scripts/echo.sh"]\n'
                           'action = "other"\n[outbox.kinds."pr.open"]\nsender = "github.pr_open"\n'
                           '[outbox.kinds."upstream.issue"]\nsender = "github.issue_create"\n'
                           '[outbox.kinds."nudge"]\nsender = "slack.message"\napproval = "auto"\nlimit = 0\n'
                           'patience = "1m"\n')
    (project.repo_root / "scripts").mkdir()
    s = project.repo_root / "scripts" / "echo.sh"
    s.write_text("#!/bin/sh\ncat\nexit 0\n")                    # prints the draft, content and all
    s.chmod(0o755)
    m = Machine(project)
    m.announce()
    m.serve()
    try:
        from orchestra_outbox.client import OutboxClient

        c = OutboxClient.for_project(project, drafter="worker/1")
        key = jira.issue()
        drafts = [
            c.draft("review.request", target={"channel": REVIEW}, fields={"link": CANARY, "reviewers": CANARY}),
            c.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content=f"{CANARY} comment"),
            c.draft("jira.comment", target={"issue": key}, content=f"{CANARY} jira"),
            c.draft("jira.task", fields={"summary": CANARY}, content=f"{CANARY} body"),
            c.draft("echo", content=f"{CANARY} script"),
            c.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content=f"{CANARY} to reject"),
            c.draft("sail.label", target={"repo": "acme/widgets", "number": 7}, content="SAIL"),
            c.draft("pr.open", target={"repo": "acme/widgets", "head": "fix-1", "base": "main"},
                    fields={"title": CANARY}, content=f"{CANARY} pr body"),
            c.draft("upstream.issue", target={"repo": "acme/widgets"}, fields={"title": CANARY, "labels": CANARY},
                    content=f"{CANARY} issue body"),
        ]
        c.draft("nudge", target={"channel": REVIEW}, content=f"{CANARY} held by a limit of 0")
        approve(web, drafts[0]["id"], content=f"{CANARY} edited")
        for d in (*drafts[1:5], *drafts[7:]):
            approve(web, d["id"])
        reject(web, drafts[5]["id"], note=f"{CANARY} note")
        slack.channels.discard("C0GENERAL")
        failing = c.draft("slack.reply", target={"channel": "C0GENERAL", "thread_ts": "1793000000.000001"},
                          content=f"{CANARY} fail")
        approve(web, failing["id"])
        settle(m, 4)
        m.review(force=True)
        m.flush()
        metrics = m.metrics() + json.dumps(m.limits())
    finally:
        m.server.stop()
    assert len(slack.posted) == 1 and CANARY in slack.posted[0]["text"]     # it was really sent
    assert [m.store.get(d["id"]).state for d in drafts].count("sent") == 8
    assert len(github.pulls) == 1 and CANARY in github.pulls[0]["title"]
    assert len(github.issues) == 1 and CANARY in github.issues[0]["title"]
    evs = bus(project)
    assert any(e["type"] == "outbox.sent" for e in evs)
    assert any(e["type"] == "outbox.rate_limit.unresolvable" for e in evs)
    assert CANARY not in str(evs)
    assert CANARY not in metrics
    assert CANARY not in caplog.text
    home = Path(project.state_dir)
    record = home / "outbox"
    for f in home.rglob("*"):
        if f.is_file() and record not in f.parents:
            assert CANARY.encode() not in f.read_bytes(), f
