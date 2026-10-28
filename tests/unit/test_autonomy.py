"""``approval = "auto"``: sent without the owner, paced by the per-recipient limit, and audited. A
draft over the limit is sent later, never held for the owner (the owner's decision of 2026-10-27)."""

from conftest import outbox_events, settle

from orchestra_outbox.machine import Machine


def label(drafter, n=7, name="needs-review", **kw):
    return drafter.draft("sail.label", target={"repo": "acme/widgets", "number": n}, content=name, **kw)


def test_an_auto_kind_sends_without_approval_and_is_audited(project, machine, drafter, github):
    d = label(drafter)
    assert d["state"] == "approved" and d["approved_by"] == "policy"
    settle(machine)
    assert github.labels[7] == ["needs-review"]
    evs = [(e["type"], e["source"], e["data"].get("by")) for e in outbox_events(project)]
    assert evs == [("outbox.draft.created", "worker/1", None), ("outbox.draft.approved", "outbox", "policy"),
                   ("outbox.sent", "outbox", None)]


def test_over_the_limit_an_auto_draft_is_approved_and_waits_for_the_window_not_for_the_owner(project, machine, drafter,
                                                                                               github):
    label(drafter)
    settle(machine)
    second = label(drafter, name="needs-review")
    assert second["state"] == "approved" and second["approved_by"] == "policy"   # never the owner's to decide
    settle(machine)
    assert len([r for r in github.requests if r[0] == "POST"]) == 1               # the window allows one
    got = drafter.get(second["id"])
    assert got["state"] == "approved" and got["held"] == "rate_limit" and got["next_attempt_at"] > got["held_since"]
    [held] = [e for e in outbox_events(project) if e["type"] == "outbox.draft.held"]
    assert held["data"]["reason"] == "rate_limit" and held["data"]["to"] == "worker/1"
    assert abs(held["data"]["until"] - got["next_attempt_at"]) < 0.01


def test_the_limit_is_per_recipient(machine, drafter, github):
    github.pr(8)
    a, b = label(drafter, 7), label(drafter, 8)
    settle(machine)
    assert drafter.get(a["id"])["state"] == drafter.get(b["id"])["state"] == "sent"


def test_the_limit_counts_every_send_of_the_kind_whoever_approved_it(make_project, web):
    """A ping the owner approved today still counts: the person gets at most one a day."""
    from conftest import approve

    t = [1_793_000_000.0]
    project = make_project('\n[outbox.kinds."nudge"]\nsender = "slack.message"\nlimit = 1\nping = true\n'
                           '[outbox.kinds."nudge.auto"]\nsender = "slack.message"\napproval = "auto"\nlimit = 1\n')
    m = Machine(project, clock=lambda: t[0])
    first = m.outbox.request({"drafter": "pacer", "kind": "nudge", "target": {"channel": "C0REVIEW"},
                              "content": "hi", "recipients": ["U0ANA"]})[0]
    second = m.outbox.request({"drafter": "pacer", "kind": "nudge", "target": {"channel": "C0GENERAL"},
                               "content": "hi again", "recipients": ["U0ANA"]})[0]
    other = m.outbox.request({"drafter": "pacer", "kind": "nudge", "target": {"channel": "C0GENERAL"},
                              "content": "hi", "recipients": ["U0BO"]})[0]
    for d in (first, second, other):
        approve(web, d.id)
    settle(m)
    states = [m.store.get(d.id) for d in (first, second, other)]
    assert [(d.state, d.held) for d in states] == [("sent", None), ("approved", "rate_limit"), ("sent", None)]


def test_after_the_window_the_waiting_draft_is_sent(make_project):
    t = [1_793_000_000.0]
    project = make_project()
    m = Machine(project, clock=lambda: t[0])
    body = {"drafter": "pacer", "kind": "sail.label", "target": {"repo": "acme/widgets", "number": 7},
            "content": "needs-review"}
    a = m.outbox.request(body)[0]
    settle(m)
    t[0] += 3600
    b = m.outbox.request(body)[0]
    settle(m)
    assert (m.store.get(a.id).state, m.store.get(b.id).state) == ("sent", "approved")
    t[0] += 86_400 - 3600 - 1
    settle(m)
    assert m.store.get(b.id).state == "approved"                   # not a second early
    t[0] += 2
    settle(m)
    sent = m.store.get(b.id)
    assert sent.state == "sent" and sent.held is None and sent.sending_since >= a.decided_at + 86_400
