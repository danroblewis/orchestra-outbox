"""A draft's whole life: a machine requests it, the owner decides on the page's API, the machine sends."""

from conftest import CANARY, approve, outbox_events, reject, settle
from fake_slack import REVIEW

from orchestra_outbox.client import OutboxError


def test_a_machine_drafts_the_owner_approves_and_slack_gets_exactly_that_message_once(project, machine, drafter, web, slack):
    d = drafter.draft("review.request", target={"channel": REVIEW}, fields={"link": "https://gh/acme/widgets/pull/7",
                      "reviewers": "@ana"}, causationid="ticket-42-ready", correlationid="ticket-42")
    assert d["state"] == "drafted" and d["content"] == "RR: https://gh/acme/widgets/pull/7 cc: @ana"
    settle(machine)
    assert slack.posted == []                                      # nothing goes before the owner approves
    state = web.get("/api/plugins/outbox/state").json()
    assert [w["id"] for w in state["waiting"]] == [d["id"]]
    approve(web, d["id"])
    settle(machine)
    settle(machine)
    assert [(m["channel"], m["text"]) for m in slack.posted] == [(REVIEW, "RR: https://gh/acme/widgets/pull/7 cc: @ana")]
    assert drafter.get(d["id"])["state"] == "sent"
    evs = outbox_events(project)
    assert [(e["type"], e["source"]) for e in evs] == [
        ("outbox.draft.created", "worker/1"), ("outbox.draft.approved", "person.owner"), ("outbox.sent", "outbox")]
    assert all(e["subject"] == d["id"] for e in evs)
    assert evs[0]["causationid"] == "ticket-42-ready" and evs[0]["correlationid"] == "ticket-42"
    assert evs[1]["causationid"] == evs[0]["id"] and evs[2]["causationid"] == evs[1]["id"]
    assert evs[2]["data"]["remote"]["channel"] == REVIEW and evs[2]["data"]["to"] == "worker/1"
    assert evs[1]["data"] == {"draft": d["id"], "kind": "review.request", "to": "worker/1", "by": "owner", "edited": False}


def test_an_edited_approval_sends_the_edit(project, machine, drafter, web, slack):
    d = drafter.draft("review.request", target={"channel": REVIEW}, fields={"link": "L", "reviewers": "@bo"})
    approve(web, d["id"], content="RR: L cc: @bo @cy (edited)")
    settle(machine)
    assert [m["text"] for m in slack.posted] == ["RR: L cc: @bo @cy (edited)"]
    got = drafter.get(d["id"])
    assert got["edited"] and got["original"] == "RR: L cc: @bo" and got["content"] == "RR: L cc: @bo @cy (edited)"
    approved = [e for e in outbox_events(project) if e["type"] == "outbox.draft.approved"]
    assert approved[0]["data"]["edited"] is True


def test_a_rejection_tells_the_drafter_why(project, machine, drafter, web, slack):
    d = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="LGTM, merging")
    reject(web, d["id"], reason="content", note="Don't say merging; I merge.")
    settle(machine)
    [ev] = [e for e in outbox_events(project) if e["type"] == "outbox.draft.rejected"]
    assert ev["source"] == "person.owner" and ev["data"]["to"] == "worker/1"
    assert ev["data"]["reason"] == "content" and ev["data"]["note"] is True
    assert "merging" not in str(ev)                               # the note is not on the bus...
    assert drafter.get(d["id"])["note"] == "Don't say merging; I merge."    # ...the drafter reads it here
    assert slack.posted == []


def test_a_draft_decided_once_cannot_be_decided_again(machine, drafter, web):
    d = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="x")
    approve(web, d["id"])
    assert web.post(f"/api/plugins/outbox/drafts/{d['id']}/reject", json={"reason": "content"}).status_code == 409
    assert web.post(f"/api/plugins/outbox/drafts/{d['id']}/approve", json={}).status_code == 409


def test_the_page_writes_need_json(machine, drafter, web):
    d = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="x")
    r = web.post(f"/api/plugins/outbox/drafts/{d['id']}/approve", data={"content": "y"})
    assert r.status_code == 415


def test_a_kind_with_no_approval_entry_always_waits(project, machine, drafter, web, github):
    d = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="Fixed in 3f2a.")
    for _ in range(5):
        machine.step()
    assert drafter.get(d["id"])["state"] == "drafted" and github.comments == []
    assert machine.settings.kinds["pr.comment"].approval == "always"


def test_the_same_key_returns_the_same_draft(machine, drafter):
    a = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="x", key="k1")
    b = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="x", key="k1")
    assert a["id"] == b["id"]


def test_bad_drafts_are_refused_with_the_reason(machine, drafter):
    cases = [
        ({"kind": "github.approve", "target": {}}, "unknown kind"),
        ({"kind": "review.request", "target": {"channel": REVIEW}, "fields": {"link": "L"}}, "reviewers"),
        ({"kind": "pr.comment", "target": {"repo": "acme/widgets"}, "content": "x"}, "target.number"),
        ({"kind": "pr.comment", "target": {"repo": "acme/widgets", "number": 7}}, "needs content"),
    ]
    for body, why in cases:
        try:
            drafter.draft(body["kind"], target=body["target"], content=body.get("content"), fields=body.get("fields"))
        except OutboxError as e:
            assert e.status == 400 and why in e.error, (body, e.error)
        else:
            raise AssertionError(f"{body} was taken")


def test_neither_the_owner_nor_the_outbox_can_draft(project, machine):
    from orchestra_outbox.client import OutboxClient

    for who in ("person.owner", "outbox"):
        try:
            OutboxClient.for_project(project, drafter=who).draft("pr.comment", target={"repo": "acme/widgets", "number": 7},
                                                                 content=CANARY)
        except OutboxError as e:
            assert e.status == 400
        else:
            raise AssertionError(who)


def test_the_api_needs_the_projects_token(machine):
    from orchestra_outbox.client import OutboxClient

    c = OutboxClient(machine.server.url, "wrong", drafter="worker/1")
    try:
        c.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="x")
    except OutboxError as e:
        assert e.status == 401
    else:
        raise AssertionError("taken without the token")
