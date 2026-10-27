"""``approval = "auto"``: sent without the owner, but never over the per-recipient limit, and audited."""

from conftest import approve, outbox_events, settle


def label(drafter, n=7, name="SAIL", **kw):
    return drafter.draft("sail.label", target={"repo": "acme/widgets", "number": n}, content=name, **kw)


def test_an_auto_kind_sends_without_approval_and_is_audited(project, machine, drafter, github):
    d = label(drafter)
    assert d["state"] == "approved" and d["approved_by"] == "policy"
    settle(machine)
    assert github.labels[7] == ["SAIL"]
    evs = [(e["type"], e["source"], e["data"].get("by")) for e in outbox_events(project)]
    assert evs == [("outbox.draft.created", "worker/1", None), ("outbox.draft.approved", "outbox", "policy"),
                   ("outbox.sent", "outbox", None)]


def test_the_limit_holds_a_second_auto_send_to_the_same_recipient_for_the_owner(project, machine, drafter, web, github):
    label(drafter)
    settle(machine)
    second = label(drafter, name="SAIL")
    assert second["state"] == "drafted" and second["held"] == "rate_limit"
    settle(machine)
    labelled = [r for r in github.requests if r[0] == "POST"]
    assert len(labelled) == 1                                     # the policy sent one, and held the second
    held = [e for e in outbox_events(project) if e["type"] == "outbox.draft.held"]
    assert held[0]["data"] == {"draft": second["id"], "kind": "sail.label", "to": "worker/1", "reason": "rate_limit"}
    approve(web, second["id"])                                    # the owner may still send it: their own act
    settle(machine)
    assert len([r for r in github.requests if r[0] == "POST"]) == 2


def test_the_limit_is_per_recipient(machine, drafter, github):
    github.pr(8)
    a, b = label(drafter, 7), label(drafter, 8)
    assert a["state"] == b["state"] == "approved"


def test_the_limit_counts_the_owners_own_approvals_of_that_kind(make_project):
    """A ping the owner approved today still counts: the person gets at most one a day."""
    from orchestra_outbox.machine import Machine

    project = make_project('\n[outbox.kinds."nudge"]\nsender = "slack.message"\napproval = "auto"\nlimit = 1\n'
                           '[outbox.kinds."nudge.manual"]\nsender = "slack.message"\n')
    m = Machine(project)
    first = m.outbox.request({"drafter": "pacer", "kind": "nudge", "target": {"channel": "C0REVIEW"},
                              "content": "hi", "recipients": ["U0ANA"]})[0]
    second = m.outbox.request({"drafter": "pacer", "kind": "nudge", "target": {"channel": "C0GENERAL"},
                               "content": "hi again", "recipients": ["U0ANA"]})[0]
    other = m.outbox.request({"drafter": "pacer", "kind": "nudge", "target": {"channel": "C0GENERAL"},
                              "content": "hi", "recipients": ["U0BO"]})[0]
    assert (first.state, second.state, second.held, other.state) == ("approved", "drafted", "rate_limit", "approved")


def test_an_auto_kind_after_the_window_sends_again(make_project):
    from orchestra_outbox.machine import Machine

    t = [1_793_000_000.0]
    project = make_project()
    m = Machine(project, clock=lambda: t[0])
    body = {"drafter": "pacer", "kind": "sail.label", "target": {"repo": "acme/widgets", "number": 7}, "content": "SAIL"}
    assert m.outbox.request(body)[0].state == "approved"
    t[0] += 3600
    assert m.outbox.request(body)[0].state == "drafted"
    t[0] += 86_400
    assert m.outbox.request(body)[0].state == "approved"
