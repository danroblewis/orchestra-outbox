"""The page as drawn from the state the API serves: one name for approval by configuration, each decided
draft's trail with its times, and every time through the core's ``Orchestra.when``. Runs the page's
script under Node with the core's helpers stubbed; skips where Node is not installed."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import approve, reject, settle

STATIC = Path(__file__).parents[2] / "orchestra_outbox" / "static"

# The core's globals the page uses (app.js), a DOM of ids, and fetch answering /state with the given state.
# Orchestra.when is a marker that shows how it was called, so the test sees every time went through it.
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [js, state] = [fs.readFileSync(process.argv[1], "utf8"), JSON.parse(fs.readFileSync(0, "utf8"))];
const els = {};
const ctx = {
  esc: (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])),
  ago: (s) => `${Math.round(s)}s`,
  $: (id) => (els[id] = els[id] || { id, innerHTML: "", textContent: "", classList: { toggle() {} }, addEventListener() {} }),
  Orchestra: { when: (t, o = {}) => `T(${t}${o.date ? ",date" : ""}${o.seconds ? ",seconds" : ""})` },
  fetch: async () => ({ ok: true, json: async () => state }),
  setInterval: () => 0, console,
};
vm.runInNewContext(js, ctx);
setTimeout(() => process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(els).map(([k, v]) => [k, v.innerHTML])))), 50);
"""


def draw(state: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", HARNESS, str(STATIC / "outbox.js")], input=json.dumps(state),
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_the_page_names_approval_by_configuration_and_shows_each_trail(project, machine, drafter, web):
    auto = drafter.draft("sail.label", target={"repo": "acme/widgets", "number": 7}, content="ready")
    mine = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 7}, content="a")
    no = drafter.draft("pr.comment", target={"repo": "acme/widgets", "number": 8}, content="b")
    approve(web, mine["id"])
    reject(web, no["id"])
    settle(machine)
    state = web.get("/api/plugins/outbox/state").json()
    rows = {d["id"]: d for d in state["recent"]}
    for d in (auto, mine):                                 # the API serves every time the trail needs
        r = rows[d["id"]]
        assert r["state"] == "sent" and r["created_at"] and r["decided_at"] and r["sent_at"]
    assert rows[no["id"]]["state"] == "rejected" and rows[no["id"]]["decided_at"] and not rows[no["id"]]["sent_at"]

    html = draw(state)
    recent = html["ob-recent"].split('<details class="ob-row">')[1:]
    by_id = {d["id"]: next(h for h in recent if f"T({rows[d['id']]['created_at']},date,seconds)" in h) for d in (auto, mine, no)}

    def t(d, k):
        return f"T({rows[d['id']][k]},date,seconds)"

    assert f"drafted {t(auto, 'created_at')} · approved by configuration {t(auto, 'decided_at')} · sent {t(auto, 'sent_at')}" in by_id[auto["id"]]
    assert f"drafted {t(mine, 'created_at')} · approved by you {t(mine, 'decided_at')} · sent {t(mine, 'sent_at')}" in by_id[mine["id"]]
    assert f"drafted {t(no, 'created_at')} · rejected {t(no, 'decided_at')}" in by_id[no["id"]]
    assert "approved" not in by_id[no["id"]] and "sent " not in by_id[no["id"]]
    assert '<span class="ob-badge">approved by configuration</span>' in by_id[auto["id"]]
    kinds = html["ob-kinds"]
    assert "<td>by configuration</td>" in kinds and "<td>by you</td>" in kinds
    for page in (html["ob-recent"], kinds):                 # the audit log shows the bus's data as it is
        assert ">auto<" not in page and "policy" not in page and "automatic" not in page


def test_the_page_says_one_name_and_formats_no_time_itself():
    js, page = (STATIC / "outbox.js").read_text(), (STATIC / "outbox.html").read_text()
    assert "toLocaleString" not in js and "Orchestra.when(t, { date: true, seconds: true })" in js
    assert "approved by configuration" in page and "set to <code>auto</code>" not in page
