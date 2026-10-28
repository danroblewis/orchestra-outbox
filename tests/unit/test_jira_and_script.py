"""The Jira Data Center sender (comment, transition, create from a template) and the script sender."""

import json

from conftest import approve, outbox_events, settle

from orchestra_outbox.machine import Machine


def force_due(m: Machine) -> None:
    with m.store.tx() as c:
        c.execute("UPDATE drafts SET next_attempt_at = NULL")


def test_a_jira_comment_is_sent_with_the_pat_and_found_after_a_lost_answer(machine, drafter, web, jira):
    key = jira.issue()
    d = drafter.draft("jira.comment", target={"issue": key}, content="Deployed to staging; usefulness: high.")
    approve(web, d["id"])
    jira.lose_next = 1
    machine.step()
    force_due(machine)
    machine.step()
    assert [c["body"] for c in jira.issues[key]["comments"]] == ["Deployed to staging; usefulness: high."]
    assert drafter.get(d["id"])["state"] == "sent"
    assert all(r[4] == f"Bearer {jira.TOKEN}" for r in jira.requests)


def test_a_jira_transition_by_name_from_the_template(machine, drafter, web, jira):
    key = jira.issue(status="In Progress")
    d = drafter.draft("jira.done", target={"issue": key})
    assert d["content"] == "Done"
    approve(web, d["id"])
    settle(machine)
    assert jira.issues[key]["status"] == "Done" and drafter.get(d["id"])["remote"] == {"issue": key, "transition": "31"}


def test_a_jira_transition_not_offered_fails_for_good(machine, drafter, web, jira):
    key = jira.issue(status="Done")
    d = drafter.draft("jira.done", target={"issue": key})
    approve(web, d["id"])
    settle(machine)
    assert drafter.get(d["id"])["error"] == "jira:transition_not_offered"


def test_a_jira_issue_is_created_from_the_kinds_payload_and_the_approved_text(machine, drafter, web, jira):
    d = drafter.draft("jira.task", fields={"summary": "Rotate the payments secret"}, content="It expires on 11-30.")
    approve(web, d["id"], content="It expires on 2026-11-30. Rotate it before.")
    jira.lose_next = 1
    machine.step()
    force_due(machine)
    machine.step()
    created = [i for i in jira.issues.values() if i["reporter"] == "dlewis"]
    assert len(created) == 1
    f = created[0]["fields"]
    assert f == {"project": {"key": "OPS"}, "issuetype": {"name": "Task"}, "summary": "Rotate the payments secret",
                 "description": "It expires on 2026-11-30. Rotate it before.", "labels": ["orchestra"]}
    assert drafter.get(d["id"])["remote"]["issue"] == created[0]["key"]


def test_a_jira_create_without_a_field_its_payload_needs_is_refused_when_drafted(machine, drafter):
    from orchestra_outbox.client import OutboxError

    try:
        drafter.draft("jira.task", content="no summary field")
    except OutboxError as e:
        assert e.status == 400 and "summary" in e.error
    else:
        raise AssertionError("taken")


SCRIPT = """#!/bin/sh
cat > "$OUT_DIR/stdin-$OUTBOX_ATTEMPT.json"
echo "attempt $OUTBOX_ATTEMPT for $OUTBOX_DRAFT_ID"
exit "$(cat "$OUT_DIR/exit-$OUTBOX_ATTEMPT" 2>/dev/null || echo 0)"
"""


def script_project(make_project, tmp_path, monkeypatch, timeout="30s"):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("OUT_DIR", str(out))
    p = make_project(f'\n[outbox.kinds."port.request"]\nsender = "script"\ncommand = ["scripts/port.sh", "--release"]\n'
                     f'action = "other"\ntarget = ["repo", "branch"]\ntimeout = "{timeout}"\n')
    (p.repo_root / "scripts").mkdir()
    s = p.repo_root / "scripts" / "port.sh"
    s.write_text(SCRIPT)
    s.chmod(0o755)
    return p, out


def test_a_script_sender_receives_the_approved_draft_on_stdin_and_its_exit_is_recorded(make_project, tmp_path,
                                                                                         monkeypatch, web):
    project, out = script_project(make_project, tmp_path, monkeypatch)
    m = Machine(project)
    d, _ = m.outbox.request({"drafter": "worker/2", "kind": "port.request", "content": "Port 3f2a to release/9",
                             "target": {"repo": "acme/widgets", "branch": "release/9"}, "fields": {"sha": "3f2a"},
                             "causationid": "ev-1"})
    approve(web, d.id, content="Port 3f2a to release/9 (approved)")
    settle(m)
    got = json.loads((out / "stdin-1.json").read_text())
    assert got["id"] == d.id and got["content"] == "Port 3f2a to release/9 (approved)" and got["edited"] is True
    assert got["target"] == {"repo": "acme/widgets", "branch": "release/9"} and got["fields"] == {"sha": "3f2a"}
    assert got["causationid"] == "ev-1" and got["drafter"] == "worker/2" and got["attempt"] == 1
    sent = m.store.get(d.id)
    assert sent.state == "sent" and sent.remote == {"exit": 0} and sent.output.strip() == f"attempt 1 for {d.id}"
    [ev] = [e for e in outbox_events(project) if e["type"] == "outbox.sent"]
    assert ev["data"]["remote"] == {"exit": 0} and "attempt 1" not in json.dumps(ev)
    created = next(e for e in outbox_events(project) if e["type"] == "outbox.draft.created")
    assert "target" not in created["data"]                       # a script's target is its own; not on the bus


def test_a_script_that_exits_75_is_run_again_and_any_other_failure_is_final(make_project, tmp_path, monkeypatch, web):
    project, out = script_project(make_project, tmp_path, monkeypatch)
    (out / "exit-1").write_text("75")
    m = Machine(project)
    d, _ = m.outbox.request({"drafter": "w", "kind": "port.request", "content": "x",
                             "target": {"repo": "a/b", "branch": "r"}})
    approve(web, d.id)
    m.step()
    assert m.store.get(d.id).state == "sending" and m.store.get(d.id).error == "script:exit_75"
    force_due(m)
    m.step()
    assert m.store.get(d.id).state == "sent" and (out / "stdin-2.json").exists()

    (out / "exit-1").write_text("3")
    e, _ = m.outbox.request({"drafter": "w", "kind": "port.request", "content": "y",
                             "target": {"repo": "a/b", "branch": "r"}})
    approve(web, e.id)
    settle(m)
    failed = m.store.get(e.id)
    assert failed.state == "failed" and failed.error == "script:exit_3" and "attempt 1" in failed.output


def test_a_script_kind_needs_its_target_keys(make_project, tmp_path, monkeypatch):
    from orchestra_outbox.kinds import DraftError

    project, _ = script_project(make_project, tmp_path, monkeypatch)
    m = Machine(project)
    try:
        m.outbox.request({"drafter": "w", "kind": "port.request", "content": "x", "target": {"repo": "a/b"}})
    except DraftError as e:
        assert "branch" in str(e)
    else:
        raise AssertionError("taken")


def test_a_script_that_hangs_is_stopped_and_tried_again(make_project, tmp_path, monkeypatch, web):
    project, _out = script_project(make_project, tmp_path, monkeypatch, timeout="1s")
    s = project.repo_root / "scripts" / "port.sh"
    s.write_text("#!/bin/sh\ncat >/dev/null\nsleep 30\n")
    m = Machine(project)
    d, _ = m.outbox.request({"drafter": "w", "kind": "port.request", "content": "x", "target": {"repo": "a/b", "branch": "r"}})
    approve(web, d.id)
    m.step()
    assert m.store.get(d.id).state == "sending" and m.store.get(d.id).error == "script:timeout"
