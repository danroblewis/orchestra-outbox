# orchestra-outbox: architecture

How the Outbox is built. `SPEC.md` is the design; this is the map of the code.

## 1. The picture

```mermaid
flowchart LR
    subgraph drafters["Drafting machines"]
        W["agents, programs, the pacer"]
    end
    subgraph machine["outbox.sender (python -m orchestra_outbox run)"]
        API["server.py: POST /drafts, GET /drafts/id, /metrics"]
        SVC["service.py: request, policy"]
        DIS["dispatch.py: claim, find, send"]
        AUD["audit.py: publish the audit rows"]
        SND["senders/: slack, github, jira, script"]
    end
    subgraph web["orchestra web (the plugin's router)"]
        PAGE["/outbox page"]
        WAPI["web.py: state, approve, reject"]
    end
    DB[("outbox.db (SQLite, 0600): drafts + audit")]
    BUS[("Orchestra's bus")]
    STORE[("metrics store")]
    EXT["Slack, GitHub, Jira DC, scripts"]
    OWNER(["the owner"])

    W -- "HTTP + project token" --> API --> SVC --> DB
    OWNER --> PAGE --> WAPI -- "approve / reject" --> DB
    DIS -- "approved drafts" --> DB
    DIS --> SND -- "as the owner" --> EXT
    AUD -- "unpublished rows" --> DB
    AUD -- "metadata only" --> BUS
    BUS -- "outbox.draft.rejected, outbox.sent ... (data.to)" --> W
    API -- "/metrics" --> INSTR["instrument outbox.outbox"] --> STORE
```

Two processes write the record: the machine (drafts, policy, sends, publishing) and the web server
(the owner's decisions). Only the machine holds tokens, calls other systems, and publishes.

## 2. Modules

| Module | What |
|---|---|
| `__init__.py` | the names: plugin `outbox`, machine `outbox`, default owner `owner` |
| `config.py` | `[outbox]`: systems, kinds, checked key by key (`problems`, `settings`) |
| `check.py` | `orchestra-outbox check`: the config check and the environment's warnings, for people and the Supervisor's tools |
| `kinds.py` | the built-in senders' specs: target, content, default recipient, idempotent |
| `templates.py` | `$name` templates for content and payloads (`string.Template`) |
| `store.py` | the record: drafts, the audit log (also the queue of bus events), conditional moves, counts |
| `service.py` | `Outbox`: a machine's request (validation, template, policy, limit), the owner's approve and reject |
| `dispatch.py` | `Dispatcher`: claim, `find` before a retry, send, record sent/failed/later, back-off |
| `senders/http.py` | JSON over HTTP, same-origin only, every failure a `SendError` code |
| `senders/slack.py`, `github.py`, `jira.py`, `script.py` | the senders (SPEC §6) |
| `credentials.py` | tokens from the environment or owner-only files (SPEC §9) |
| `audit.py` | the event contract and `flush` |
| `measures.py` | `/metrics` from the record |
| `server.py` | the machine's HTTP API |
| `machine.py` | the process: announce, serve, every 0.5 s reload/flush/dispatch/flush |
| `client.py` | `OutboxClient` for Python drafters |
| `plugin.py` | the entry point: page, router, declared machine, process, instrument |
| `web.py` | the page's API (rule 9's human path) |
| `static/` | the page: `outbox.html`, `outbox.js`, `outbox.css` |

## 3. One draft, in order

```mermaid
sequenceDiagram
    autonumber
    participant D as drafter (worker/1)
    participant M as outbox machine
    participant DB as outbox.db
    participant B as bus
    participant W as web (owner)
    participant S as Slack
    D->>M: POST /drafts {kind, target, fields, causationid, key}
    M->>DB: insert draft + audit(created, source worker/1)
    M-->>D: 201 {id, state: drafted}
    M->>B: outbox.draft.created (source worker/1)
    W->>DB: approve (edited?) + audit(approved, source person.owner)
    M->>B: outbox.draft.approved (source person.owner)
    M->>DB: claim: approved → sending
    M->>S: chat.postMessage (+ metadata: draft id)
    S-->>M: ok, ts
    M->>DB: sent, remote + audit(sent, source outbox)
    M->>B: outbox.sent (data.to = worker/1)
```

If step 9's answer is lost, the draft stays `sending`; the next try reads the channel for the
message carrying the draft's id and records it sent without posting again (`dispatch.py`).

## 4. Decisions, and why

- **Drafting by API, not by bus**: the bus carries no text. SPEC §3.
- **The machine's API, not the web server's**: the machine runs where the drafters run, and the
  web server is the owner's, perhaps tunnelled; it should not also be the machines' door.
- **The audit log is the event queue** (a transactional outbox): a decision and its event cannot
  part. The web server never publishes; the machine does, within half a second, with a stable id.
- **Approval writes, the machine sends**: one process holds every write token (story placement
  §2.2: "one place holds every write token and every audit record").
- **The fakes are copied, not imported** from `~/orchestra-bridges`: theirs are read-only and in
  another repository, and whether the senders' API code should be shared with the bridges is the
  owner's open question 4. The senders' HTTP code is the bridges' pattern (same-origin redirects,
  codes not bodies), written again here.
- **SQLite**: two processes, conditional updates, durable before `201` is answered.
- **Rate limits count approvals, not sends**, and bind only the policy: the owner may approve a
  held draft.

## 5. Tests

| File | What |
|---|---|
| `tests/fake_http.py` | the fakes' server: token, scripted failures, `lose_next` (act, then drop the answer), `asleep` |
| `tests/fake_slack.py`, `fake_github.py`, `fake_jira.py` | the three APIs, in their shapes |
| `tests/unit/test_flow.py` | draft → approve on the page's API → Slack gets it once; edit; reject tells the drafter; refusals |
| `tests/unit/test_autonomy.py` | `auto` sends and is audited; the limit holds; per recipient; the window |
| `tests/unit/test_idempotent.py` | lost answers (Slack, thread, GitHub), 5xx, restart mid-send, the bus down |
| `tests/unit/test_jira_and_script.py` | Jira comment, transition, create; the script sender's contract |
| `tests/unit/test_canary.py` | no content on the bus, in metrics, logs or any other file |
| `tests/unit/test_config_check.py` | the contract, the check (CLI and JSON), SPEC's example, reload |
| `tests/unit/test_plugin.py` | the declaration, Orchestra loading it, the page, the metrics, file modes |
| `tests/e2e/test_orchestra_run.py` | `orchestra run` loads it; draft, approve, sent once; the instrument stores; a restart sends nothing twice |
