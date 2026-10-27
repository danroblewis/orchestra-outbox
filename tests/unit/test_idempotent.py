"""A send is never repeated blindly: a lost answer, a restart mid-send, a draft never lost."""

from conftest import approve, outbox_events, settle
from fake_slack import REVIEW

from orchestra_outbox.machine import Machine


def force_due(m: Machine) -> None:
    """Skip the back-off: make every draft due now."""
    with m.store.tx() as c:
        c.execute("UPDATE drafts SET next_attempt_at = NULL")


def test_a_lost_answer_from_slack_is_found_not_sent_again(project, machine, drafter, web, slack):
    d = drafter.draft("review.request", target={"channel": REVIEW}, fields={"link": "L", "reviewers": "@a"})
    approve(web, d["id"])
    slack.lose_next = 1
    machine.step()
    assert len(slack.posted) == 1 and drafter.get(d["id"])["state"] == "sending"
    force_due(machine)
    machine.step()
    assert len(slack.posted) == 1 and drafter.get(d["id"])["state"] == "sent"
    sent = [e for e in outbox_events(project) if e["type"] == "outbox.sent"]
    assert len(sent) == 1 and sent[0]["data"]["attempts"] == 2


def test_a_lost_answer_for_a_thread_reply_is_found(project, machine, drafter, web, slack):
    root = slack.say(REVIEW, "can someone look?")
    d = drafter.draft("slack.reply", target={"channel": REVIEW, "thread_ts": root}, content="On it.")
    approve(web, d["id"])
    slack.lose_next = 1
    machine.step()
    force_due(machine)
    machine.step()
    mine = slack.posted
    assert len(mine) == 1 and mine[0]["thread_ts"] == root and drafter.get(d["id"])["state"] == "sent"


def test_a_lost_answer_from_github_is_found_not_sent_again(project, machine, drafter, web, github):
    d = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="Fixed.")
    approve(web, d["id"])
    github.lose_next = 1
    machine.step()
    force_due(machine)
    machine.step()
    assert len(github.comments_on(7)) == 1 and drafter.get(d["id"])["state"] == "sent"
    assert github.comments_on(7)[0]["body"].startswith("Fixed.\n\n<!-- orchestra-outbox:")


def test_a_slack_error_that_says_no_is_failed_for_good(project, machine, drafter, web, slack):
    slack.channels.discard(REVIEW)
    d = drafter.draft("review.request", target={"channel": REVIEW}, fields={"link": "L", "reviewers": "@a"})
    approve(web, d["id"])
    settle(machine)
    got = drafter.get(d["id"])
    assert got["state"] == "failed" and got["error"] == "slack:channel_not_found"
    [f] = [e for e in outbox_events(project) if e["type"] == "outbox.send_failed"]
    assert f["data"]["error"] == "slack:channel_not_found" and f["data"]["to"] == "worker/1"


def test_a_5xx_is_tried_again_and_sent_once(project, machine, drafter, web, github):
    d = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="x")
    approve(web, d["id"])
    github.fail_next = [(502, {}, {"message": "Bad Gateway"})]
    machine.step()
    assert drafter.get(d["id"])["state"] == "sending" and drafter.get(d["id"])["error"] == "http_502"
    force_due(machine)
    machine.step()
    assert len(github.comments_on(7)) == 1 and drafter.get(d["id"])["state"] == "sent"


def test_a_restart_neither_loses_nor_double_sends(project, slack, web):
    """The machine dies after Slack took the message and before it recorded it: the next machine
    finds it. A draft requested before the restart and approved after it is sent once."""
    from orchestra_outbox.client import OutboxClient

    first = Machine(project)
    first.serve()
    client = OutboxClient.for_project(project, drafter="worker/1")
    a = client.draft("review.request", target={"channel": REVIEW}, fields={"link": "A", "reviewers": "@a"})
    b = client.draft("review.request", target={"channel": REVIEW}, fields={"link": "B", "reviewers": "@b"})
    approve(web, a["id"])
    first.flush()
    slack.lose_next = 1                       # Slack posts; the answer never comes back, as if we died
    first.dispatcher.step()
    first.server.stop()
    del first

    second = Machine(project)                 # a new process: same record
    second.serve()
    try:
        force_due(second)
        approve(web, b["id"])
        settle(second)
        assert sorted(m["text"] for m in slack.posted) == ["RR: A cc: @a", "RR: B cc: @b"]
        assert {second.store.get(a["id"]).state, second.store.get(b["id"]).state} == {"sent"}
        types = [e["type"] for e in outbox_events(project)]
        assert types.count("outbox.sent") == 2 and types.count("outbox.draft.created") == 2
        ids = [e["id"] for e in outbox_events(project)]
        assert len(ids) == len(set(ids))
    finally:
        second.server.stop()


def test_the_audit_waits_for_the_bus_and_is_published_once(project, machine, drafter):
    class Down:
        def publish(self, *a, **k):
            raise ConnectionError("bus down")

    real = machine.events
    machine.events = Down()
    d = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="x")
    machine.step()
    assert outbox_events(project) == []
    assert [r["published"] for r in machine.store.log()] == [False]
    machine.events = real
    machine.step()
    machine.step()
    assert [e["type"] for e in outbox_events(project)] == ["outbox.draft.created"]
    assert outbox_events(project)[0]["id"] == f"outbox:{d['id']}:1"
