"""End to end: `orchestra run` loads the plugin and keeps the outbox running; a machine drafts through
the API, the owner approves on the page's API, the fake Slack gets the message once; the plugin's
instrument stores the outbox's measurements; stopping `orchestra run` stops the outbox, and a
restart sends nothing twice."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from conftest import CANARY, approve, bus
from fake_slack import REVIEW
from orchestra.metrics import for_project

from orchestra_outbox.client import OutboxClient, OutboxError


def wait_for(check, timeout=40.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if v := check():
                return v
        except (OutboxError, OSError):
            pass
        time.sleep(0.3)
    raise AssertionError("timed out")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(project):
    orchestra = Path(sys.executable).with_name("orchestra")
    return subprocess.Popen([str(orchestra), "run"], cwd=project.repo_root, env=os.environ.copy(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)


def stop(host):
    host.send_signal(signal.SIGTERM)
    host.wait(timeout=40)


def test_orchestra_run_keeps_the_outbox_and_a_draft_is_sent_once(make_project, slack):
    from fastapi.testclient import TestClient
    from orchestra.web.app import create_app
    from orchestra.web.auth import bearer

    project = make_project(port=free_port())
    web = TestClient(create_app(project), headers=bearer(project))
    host = start(project)
    try:
        client = wait_for(lambda: OutboxClient.for_project(project, drafter="worker/1"))
        d = wait_for(lambda: client.draft("review.request", target={"channel": REVIEW},
                                          fields={"link": "https://gh/pr/7", "reviewers": CANARY}))
        assert wait_for(lambda: any(e["type"] == "outbox.draft.created" for e in bus(project)))
        assert slack.posted == []
        approve(web, d["id"])
        wait_for(lambda: client.get(d["id"])["state"] == "sent")
        assert [m["text"] for m in slack.posted] == [f"RR: https://gh/pr/7 cc: {CANARY}"]
        wait_for(lambda: any(e["type"] == "outbox.sent" for e in bus(project)))
        assert wait_for(lambda: for_project(project).latest("outbox_drafts_sent_total",
                                                            {"kind": "review.request", "instrument": "outbox.outbox"}))
        started = [e for e in bus(project) if e["type"] == "orchestra.process.started"]
        assert ("outbox.sender", "outbox") in [(e["data"]["name"], e["data"]["owner"]) for e in started]
        announced = [e for e in bus(project) if e["type"] == "orchestra.machine.started" and e["source"] == "outbox"]
        assert announced and announced[0]["data"]["package"] == "outbox"
    finally:
        stop(host)
    log = (project.logs_dir / "machines" / "outbox.sender.log").read_text()
    assert "outbox: stopped" in log and CANARY not in log
    assert CANARY not in str(bus(project))

    host = start(project)                          # a restart: nothing is sent again
    try:
        wait_for(lambda: OutboxClient.for_project(project, drafter="worker/1").get(d["id"]))
        time.sleep(2)
        assert len(slack.posted) == 1
    finally:
        stop(host)
