# orchestra-outbox

The Outbox for [Orchestra](../orchestra_2): nothing is said or done as you in another system until
you approve it.

Machines (agents, programs, the follow-up pacer) write **drafts**: a Slack message, a reply in a
thread, a pull-request comment or label, a new pull request, a Jira comment, transition or new
issue, or anything a script of yours can do (a push, a merge). Approving a pull request is the one
thing no draft may do. You see them on the **Outbox** page, edit them if you like, and approve or
reject each one. Approving sends it, once. Rejecting tells the machine that drafted it why. Every
step is an event on Orchestra's bus, with ids and codes, never the text.

What is drafted, how it is sent and whether it needs you are **configuration**: a kind per thing
you want said, each naming its sender and template. Every kind waits for your approval unless its
entry says otherwise, and you change that by asking the Supervisor.

- Design: [SPEC.md](SPEC.md). The configuration contract is SPEC §4.
- How it works inside: [ARCHITECTURE.md](ARCHITECTURE.md).
- Rules for changing it: [AGENTS.md](AGENTS.md).

## Install

```bash
cd ~/orchestra-outbox
uv sync --extra dev                # Orchestra comes from GitHub, at the commit uv.lock pins
uv run python -m pytest -q
uv run ruff check
```

To use it with a project, run `orchestra run` and `orchestra web` from this environment, or install
Orchestra with it as a tool, from GitHub with no clone:

```bash
uv tool install 'orchestra[sd] @ git+https://github.com/danroblewis/orchestra@main' --with 'orchestra-outbox @ git+https://github.com/danroblewis/orchestra-outbox'
```

Its command line comes with it as `orchestra outbox` (`orchestra outbox --help`); there is no
executable of its own.

Only someone logged in to Orchestra's web UI approves. The Outbox page and its API are behind
`orchestra web`'s login (the project's web token: open the link `orchestra web` prints, or paste
`orchestra web token` into its login page), and an approval or rejection carried by the browser's
session must come from Orchestra's own pages: a form or a script on another site gets `403`. A
program that approves for you would send `Authorization: Bearer <web token>`; nothing in the
outbox does.

## Developing against a local Orchestra

`pyproject.toml` takes Orchestra from GitHub (`orchestra @ git+https://github.com/danroblewis/orchestra@main`),
and `uv.lock` pins the commit it resolved to, so nothing needs cloning beside this repository.
`uv lock --upgrade-package orchestra` moves the pin to the newest `main` (then `uv sync --extra dev`).

To run against an Orchestra checkout instead, with its uncommitted changes, lay it over this
environment for one command:

```bash
uv run --with-editable ~/orchestra_2 python -m pytest -q      # the path to your Orchestra checkout
uv run --with-editable ~/orchestra_2 orchestra web
```

Nothing is written: `.venv`, `pyproject.toml` and `uv.lock` keep GitHub's Orchestra, and a plain
`uv run` uses it again. Inside the command, `sys.executable` and the `orchestra` beside it import
the checkout, so the processes the tests and the host start do too.

For something that runs `.venv/bin/orchestra` directly, install the checkout into `.venv` and keep
uv from syncing it away:

```bash
uv pip install -e ~/orchestra_2
uv run --no-sync python -m pytest -q          # or export UV_NO_SYNC=1
uv sync --extra dev                            # back to the locked Orchestra from GitHub
```

A plain `uv run` or `uv sync` also puts the locked Orchestra back.

## Configure

In the project's `orchestra.toml`:

```toml
[outbox]
merge_labels = ["SAIL"]               # labels that merge a pull request: the first per PR is always yours

[outbox.slack]                        # Slack's connection; its token and your account live elsewhere

[outbox.kinds."review.request"]
sender = "slack.message"
template = "RR: $link cc: $reviewers"
```

Your own kinds, channels and templates belong in your private overlay (`orchestra.local.toml` in
the project's state directory), not a shared `orchestra.toml`. Five starting kinds, all approved by
you each time, come with the outbox: `orchestra outbox example` prints them (a Slack thread reply,
"review requested", a Jira comment, a Jira transition, a Jira issue from your template).

**Your accounts and tokens** live in one place outside every repository, shared with the bridges
(SPEC §9):

```bash
mkdir -p -m 700 ~/.config/orchestra/tokens
cat > ~/.config/orchestra/accounts.toml <<'TOML'
owner = "person.daniel"             # your person id: person.<key> in the identity map
[slack]
account = "U012AB3CD"               # your member id
[github]
account = "danroblewis"
[jira]
api = "https://jira.example.com"
account = "dlewis"
TOML
chmod 600 ~/.config/orchestra/accounts.toml
printf %s 'xoxp-...' > ~/.config/orchestra/tokens/slack-token && chmod 600 ~/.config/orchestra/tokens/slack-token
```

Your approvals publish as your person id: `[outbox] owner`, else `owner` in `accounts.toml`, else
the identity map's entry (`[bridges] people`, or `[outbox] people`) that lists one of your accounts
there, which is the same `person.<key>` the bridges give your own acts.

| System | Token | Scopes / rights |
|---|---|---|
| Slack | a user token (`xoxp-`), `~/.config/orchestra/tokens/slack-token` | `chat:write`; `channels:history`, `groups:history`, `im:history`, `mpim:history` (to check a send whose answer was lost) |
| GitHub | `~/.config/orchestra/tokens/github-token`, else your `gh` login | write issues and pull requests (and contents, to open one) |
| Jira Data Center | a personal access token, `~/.config/orchestra/tokens/jira-token` | comment, transition, create in the projects you use |

Phase 1's `~/.config/orchestra-outbox/<system>-token` is still read; move it.

Check a configuration before committing it (the Supervisor's tools call the same):

```bash
uv run orchestra outbox check --repo ~/work            # exit 1 and the reasons when it is wrong
uv run orchestra outbox check --file proposed.toml --json
```

### Kinds

| `sender` | Target | Content |
|---|---|---|
| `slack.message` | `{"channel": "C…"}` | the message |
| `slack.reply` | `{"channel": "C…", "thread_ts": "…"}` | the reply |
| `github.pr_comment` | `{"repo": "owner/name", "number": 7}` | the comment |
| `github.label` | same | one label name (not `approved` or `lgtm`) |
| `github.pr_open` | `{"repo": "owner/name", "head": "branch", "base": "main"}`, and a `title` field | the description |
| `jira.comment` | `{"issue": "OPS-12"}` | the comment |
| `jira.transition` | `{"issue": "OPS-12"}` | the transition's name or id |
| `jira.create` | `{}` or `{"project": "OPS"}` | the description (`$content` in the kind's `payload`) |
| `script` | any object; `target = [...]` lists the keys required | whatever the script reads |

Per kind: `template`, `approval` (`"always"`, the default, or `"auto"`), `ping` (a ping or nudge:
drafts must name their `recipients`), `limit` and `per` (at most `limit` sends per recipient per
`per`), `patience`. SPEC §4 has every key and an example of each kind.

**Rate limits.** A draft over its limit is never dropped and never comes back to you: it is sent
when the window allows, and the same for an API that says to slow down. If waiting will not clear
the backlog (drafts arrive faster than the limit lets out, or one waits past its `patience`), the
machines that drafted them say so on the bus (`outbox.rate_limit.unresolvable`), the Outbox page
shows the limit, the backlog and a graph of its use, and you tell the Supervisor what to do.
`orchestra outbox limits` prints the same.

**What no kind does.** Approve a pull request: refused at `check`, at drafting and at sending. A
merge action (a script with `action = "github.pr_merge"`, or a label in `merge_labels`) needs your
approval the first time on each pull request, even for an `auto` kind.

### Scripts

For a workflow no built-in sender knows (`arc feature`, a company tool), make the kind's sender a
script:

```toml
[outbox.kinds."port.request"]
sender = "script"
command = ["scripts/outbox/arc-port.sh"]
action = "other"                        # what it does: other, git.push, github.pr_open, github.pr_merge
target = ["repo", "sha", "branch"]
```

A script says what it does in `action`: `other`, `git.push`, `github.pr_open`, `github.pr_merge`
(`github.pr_approve` is refused).

Once you approve a draft, the script runs in the repository root with the draft as JSON on stdin
(`id`, `content`, `target`, `fields`, ...) and `OUTBOX_DRAFT_ID` in its environment. Exit 0 is sent;
exit 75 is "try again later"; anything else is failed. **Make it idempotent on `OUTBOX_DRAFT_ID`**:
after a crash or a timeout it may run again for the same draft.

## Drafting

A machine drafts through the outbox's API (it is up while `orchestra run` is):

```bash
curl -s -X POST "http://127.0.0.1:9467/drafts" \
  -H "Authorization: Bearer $ORCHESTRA_EVENTS_TOKEN" -H "Content-Type: application/json" \
  -d '{"drafter": "worker/1", "kind": "review.request", "target": {"channel": "C0REVIEW"},
       "fields": {"link": "https://github.com/acme/widgets/pull/7", "reviewers": "@ana"},
       "causationid": "<the event that caused it>", "key": "rr:acme/widgets#7"}'
```

or in Python:

```python
from orchestra_outbox.client import OutboxClient
outbox = OutboxClient.for_project(project, drafter="worker/1")
d = outbox.draft("review.request", target={"channel": "C0REVIEW"},
                 fields={"link": url, "reviewers": "@ana"}, key=f"rr:{pr}")
```

Then listen on the bus for `outbox.draft.rejected`, `outbox.sent` and `outbox.send_failed` whose
`data.to` is your source, and read the owner's note with `GET /drafts/<id>`. SPEC §3 and §7.

## What it measures

`outbox_drafts_waiting`, `outbox_oldest_draft_waiting_seconds`, counters of drafts created,
approved (by whom, edited or not), rejected, sent and failed, per kind, and the rate limits' use
(`outbox_rate_limit`, `outbox_rate_limit_sends`, `outbox_rate_limit_backlog`,
`outbox_rate_limit_unresolvable`, per kind and recipient; `outbox_api_rate_limit_backlog` per
system), on `/metrics`, stored by the instrument `outbox.outbox`, so Orchestra graphs them.

## Limits

- Fakes, not real APIs, test the senders (`tests/fake_*.py`, mirroring the bridges' fakes). No
  opt-in contract test exists yet.
- A script is run again after a timeout or exit 75; only the script can make that safe.
- A Jira transition whose answer was lost counts as made when the issue no longer offers it.
- Autonomy is never earned automatically: the page shows the unedited approvals in a row, with
  "could be automatic: ask the Supervisor" after a long run, and you decide.
- The Supervisor does not act on a rate-limit notice yet: what it needs is SPEC §7.2.
