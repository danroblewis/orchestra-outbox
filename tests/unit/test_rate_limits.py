"""Rate limits (the owner's decision of 2026-10-27): a draft over a limit is retried when the window
allows, never over the limit, never dropped and never held for the owner; when waiting will not
clear the backlog, the drafters behind it say so on the bus, and the usage is measured."""

import json
import urllib.request

from conftest import CANARY, bus, settle
from fake_slack import REVIEW

from orchestra_outbox import limits
from orchestra_outbox.check import main
from orchestra_outbox.machine import Machine

T0 = 1_793_000_000.0
NUDGE = ('\n[outbox.kinds."nudge"]\nsender = "slack.message"\napproval = "auto"\nping = true\nlimit = 2\nper = "1h"\n'
         '{extra}')


def machine_at(make_project, extra=""):
    t = [T0]
    m = Machine(make_project(NUDGE.format(extra=extra)), clock=lambda: t[0])
    return m, t


def nudge(m, drafter="pacer", who="U0ANA", text="hi"):
    return m.outbox.request({"drafter": drafter, "kind": "nudge", "target": {"channel": REVIEW}, "content": text,
                             "recipients": [who]})[0]


def run(m, t, seconds, step=60.0):
    end = t[0] + seconds
    while t[0] < end:
        t[0] += step
        m.step()
        m.review(force=True)
        m.flush()


def test_drafts_over_the_limit_are_all_sent_later_and_never_more_than_the_limit_in_any_window(make_project, slack):
    m, t = machine_at(make_project)
    drafts = [nudge(m, text=f"hi {i}") for i in range(7)]
    other = nudge(m, who="U0BO")
    settle(m)
    assert len(slack.posted) == 3                                  # two to Ana, one to Bo; five wait
    assert all(m.store.get(d.id).state == "approved" for d in drafts[2:])
    run(m, t, 4 * 3600)
    got = [m.store.get(d.id) for d in drafts]
    assert all(d.state == "sent" for d in got) and m.store.get(other.id).state == "sent"
    assert sorted(p["text"] for p in slack.posted if p["text"].startswith("hi ")) == [f"hi {i}" for i in range(7)]
    times = sorted(d.sending_since for d in got)
    for s in times:
        assert sum(1 for x in times if s - 3600 < x <= s) <= 2, times
    assert [d.id for d in sorted(got, key=lambda d: d.sending_since)] == [d.id for d in drafts]    # in order
    held = [e for e in bus(m.project) if e["type"] == "outbox.draft.held"]
    assert len(held) == 5 and {e["data"]["reason"] for e in held} == {"rate_limit"}             # once per draft


def test_an_unresolvable_limit_is_said_by_each_drafter_behind_it_and_resolved_when_it_clears(make_project):
    m, t = machine_at(make_project)
    for i in range(4):
        nudge(m, "pacer", text=f"{CANARY} {i}")
    for i in range(3):
        nudge(m, "worker/1", text=f"{CANARY} w{i}")
    settle(m)
    m.review(force=True)
    m.flush()
    evs = [e for e in bus(m.project) if e["type"] == "outbox.rate_limit.unresolvable"]
    assert sorted(e["source"] for e in evs) == ["pacer", "worker/1"]
    resource = "ratelimit:kind=nudge,recipient=U0ANA"
    by = {e["source"]: e for e in evs}
    assert all(e["subject"] == resource for e in evs)
    d = by["pacer"]["data"]
    assert set(d) == {"resource", "scope", "system", "api", "kind", "recipient", "limit", "per", "patience",
                      "sent_in_window", "arrivals_in_window", "backlog", "backlog_total", "oldest_wait", "why", "drafts",
                      "series", "read"}
    assert (d["resource"], d["scope"], d["system"], d["api"], d["kind"], d["recipient"]) == (
        resource, "kind", "slack", "127.0.0.1", "nudge", "U0ANA")
    assert (d["limit"], d["per"], d["patience"], d["sent_in_window"], d["arrivals_in_window"]) == (2, 3600, 7200, 2, 7)
    assert (d["backlog"], d["backlog_total"], d["why"]) == (2, 5, ["outpaced"])
    assert by["worker/1"]["data"]["backlog"] == 3 and len(by["worker/1"]["data"]["drafts"]) == 3
    assert d["series"]["sends"] == "outbox_rate_limit_sends" and d["read"] == "/limits"
    assert CANARY not in json.dumps(evs)
    m.review(force=True)
    m.flush()
    assert len([e for e in bus(m.project) if e["type"] == "outbox.rate_limit.unresolvable"]) == 2     # said once
    run(m, t, 4 * 3600, step=300)
    resolved = [e for e in bus(m.project) if e["type"] == "outbox.rate_limit.resolved"]
    assert sorted(e["source"] for e in resolved) == ["pacer", "worker/1"]
    assert resolved[0]["data"]["backlog"] == 0 and resolved[0]["data"]["recipient"] == "U0ANA"
    assert resolved[0]["causationid"] in {e["id"] for e in evs}


def test_a_draft_that_waits_past_its_patience_is_unresolvable_even_when_the_backlog_is_small(make_project):
    m, t = machine_at(make_project, 'patience = "10m"\n')
    for _ in range(3):
        nudge(m)
    settle(m)
    m.review(force=True)
    assert [lim.why for lim in limits.status(m.store, m.settings, t[0])] == [[]]
    t[0] += 11 * 60
    m.review(force=True)
    m.flush()
    [ev] = [e for e in bus(m.project) if e["type"] == "outbox.rate_limit.unresolvable"]
    assert ev["source"] == "pacer" and ev["data"]["why"] == ["waited"] and ev["data"]["oldest_wait"] >= 660


def test_the_usage_is_an_instrument_series_and_on_the_api_the_cli_and_the_page(make_project, slack, web, capsys):
    m, _t = machine_at(make_project)
    for _ in range(7):
        nudge(m)
    settle(m)
    m.serve()
    try:
        text = m.metrics()
        assert 'outbox_rate_limit{kind="nudge"} 2' in text
        assert 'outbox_rate_limit_window_seconds{kind="nudge"} 3600' in text
        assert 'outbox_rate_limit_sends{kind="nudge",recipient="U0ANA"} 2' in text
        assert 'outbox_rate_limit_backlog{kind="nudge",recipient="U0ANA"} 5' in text
        assert 'outbox_rate_limit_unresolvable{kind="nudge",recipient="U0ANA"} 1' in text
        from orchestra import endpoint
        req = urllib.request.Request(m.server.url + "/limits",
                                     headers={"Authorization": f"Bearer {endpoint.token(m.project)}"})
        with urllib.request.urlopen(req) as r:
            api = json.loads(r.read())
        try:
            urllib.request.urlopen(m.server.url + "/limits")
            raise AssertionError("served without the token")
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        m.server.stop()
    [lim] = api["limits"]
    assert (lim["kind"], lim["recipient"], lim["limit"], lim["backlog"], lim["unresolvable"]) == ("nudge", "U0ANA", 2, 5,
                                                                                                  True)
    assert len(lim["usage"]) == 49 and lim["usage"][-1][1:] == [2, 5]          # [t, sends in window, waiting]
    state = web.get("/api/plugins/outbox/state").json()
    assert state["limits"]["limits"][0]["resource"] == "ratelimit:kind=nudge,recipient=U0ANA"
    assert main(["limits", "--repo", str(m.project.repo_root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["limits"][0]["backlog"] == 5
    assert main(["limits", "--repo", str(m.project.repo_root)]) == 0
    assert "UNRESOLVABLE" in (out := capsys.readouterr().out) and "nudge to U0ANA" in out   # by the real clock


def test_an_apis_rate_limit_is_waited_out_without_using_up_the_tries_and_said_when_it_lasts(make_project, slack):
    m, t = machine_at(make_project, '[outbox.kinds."post"]\nsender = "slack.message"\napproval = "auto"\n')
    slack.fail_next = [(429, {"Retry-After": "30"}, {"ok": False, "error": "ratelimited"})] * 30
    d = m.outbox.request({"drafter": "pacer", "kind": "post", "target": {"channel": REVIEW}, "content": "x"})[0]
    run(m, t, 4 * 3600, step=120)
    got = m.store.get(d.id)
    assert got.attempts > m.settings.max_attempts and got.state == "sent" and len(slack.posted) == 1
    evs = bus(m.project)
    [held] = [e for e in evs if e["type"] == "outbox.draft.held"]
    assert held["data"]["reason"] == "api_rate_limit"
    [ev] = [e for e in evs if e["type"] == "outbox.rate_limit.unresolvable"]
    assert ev["source"] == "pacer" and ev["subject"] == "ratelimit:api=slack"
    assert (ev["data"]["scope"], ev["data"]["limit"], ev["data"]["why"]) == ("api", None, ["waited"])
    assert [e["source"] for e in evs if e["type"] == "outbox.rate_limit.resolved"] == ["pacer"]
