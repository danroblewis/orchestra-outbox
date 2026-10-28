# Rules for working on the Outbox

Read `SPEC.md` first. It is the design. This file is the short list of rules that must not be
broken without updating the spec and saying so in the commit. Orchestra's own rules
(`../orchestra_2/AGENTS.md`) bind this plugin too; rules 1, 3, 5 and 9 most of all.

## Invariants

1. **Nothing is sent as the owner without the owner's approval, unless the configuration says
   so.** Every kind's approval is `"always"` unless its entry in `[outbox.kinds]` says `"auto"`.
   Never add a default, a heuristic or a code path that loosens it: autonomy is the owner's
   configuration, set by asking the Supervisor. The first merge action on each pull request is the
   owner's whatever the kind says, and no kind ever approves a pull request (SPEC §6.6).
2. **This plugin is the human path that changes the system** (Orchestra's rule 9). The Outbox
   page and its API (`web.py`) are the owner's way to approve, edit and reject drafts, behind
   Orchestra's web login (never add a route or a server that is reached without it). That is the
   only human write here. The page never sends and never holds a token: it writes the decision to
   the record, and the outbox machine acts on it.
3. **No content on the bus, in the metrics or in a log.** Events, series, log lines and errors
   carry ids, kinds, targets, codes and flags; never a draft's text, an edit, a label's name, the
   owner's note or a script's output. The text lives only in the record. The canary test
   (`tests/unit/test_canary.py`) must stay green, and covers every sender.
4. **Every change is audited, with the actor as the source.** A change to a draft and its audit
   row are one transaction; the machine publishes the row as the drafter, the owner or `outbox`.
   Never publish an outbox event any other way.
5. **A send is never repeated blindly.** A try after one whose outcome is unknown finds the draft
   first, by the mark its sender leaves (`find`). A new sender must leave a mark and find it, or be
   idempotent, or (a script) be told the draft's id so it can be.
6. **Kinds and senders are configured, not coded.** A new company workflow is a kind in config,
   or a script. A new built-in sender is for a system many owners use (as the bridges' test is),
   and it comes with a fake that speaks the real API, a `find`, and canary coverage.
7. **Tokens stay in memory, in the machine.** From the environment or an owner-only file outside
   the repository (the shared `~/.config/orchestra/`, SPEC §9; `accounts.py` stays free of the
   outbox's imports so a common library can take it), read when first needed, sent only to the
   configured API's origin, never written, logged or published. `config.py` refuses a `token` key.
8. **State lives in `~/.orchestrator/<key>/outbox/`** (the record, owner-only), except the events
   and metrics, which go through `orchestra.events` and `/metrics`. Nothing here imports
   Orchestra's web app, host or model outside tests.
9. **A limit paces; it never drops and never asks.** No draft is sent over a limit, and none over
   it is dropped or sent back to the owner: it waits for the window. When waiting will not clear a
   backlog, the drafters behind it publish `outbox.rate_limit.unresolvable` (SPEC §5.3, §7.1), a
   contract the Supervisor and programs read: change its fields only with SPEC §7.1.

## Conventions

- Python 3.12+, `uv` for everything. `uv sync --extra dev`, then `set -o pipefail; uv run python
  -m pytest -q` and `uv run ruff check`.
- Orchestra comes from GitHub (`orchestra @ git+https://github.com/danroblewis/orchestra@main`), at the
  commit `uv.lock` pins; README, "Developing against a local Orchestra". Test projects live in
  temporary plain directories with their own `ORCHESTRATOR_HOME`.
- Each system has a fake of its API in the real shapes (`tests/fake_<system>.py`), copied in the
  style of the bridges' fakes, never imported from another repository.
- The configuration contract (SPEC §4) is what the Supervisor's tools will edit: change it only
  deliberately, with `check.py`, its tests and the SPEC's example in the same commit.
- Keep modules small and single-purpose. If a file needs a section divider comment, split it.
