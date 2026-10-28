# orchestra-outbox

The Outbox for [Orchestra](../orchestra_2): nothing is said or done as you in another system until
you approve it.

Machines (agents, programs, the follow-up pacer) write **drafts**: a Slack message, a reply in a
thread, a pull-request comment or label, a Jira comment, transition or new issue, or anything a
script of yours can do. You see them on the **Outbox** page, edit them if you like, and approve or
reject each one. Approving sends it, once. Rejecting tells the machine that drafted it why. Every
step is an event on Orchestra's bus, with ids and codes, never the text.

What is drafted, how it is sent and whether it needs you are **configuration**: a kind per thing
you want said, each naming its sender and template. Every kind waits for your approval unless its
entry says otherwise, and you change that by asking the Supervisor.

- Design: [SPEC.md](SPEC.md). The configuration contract is SPEC §4.
- How it works inside: [ARCHITECTURE.md](ARCHITECTURE.md).
- Rules for changing it: [AGENTS.md](AGENTS.md).

## Install

Beside Orchestra, as the other plugins are:

```bash
cd ~/orchestra-outbox
uv sync --extra dev                # Orchestra comes editable from ../orchestra_2
uv run python -m pytest -q
uv run ruff check
```

To use it with a project, run `orchestra run` and `orchestra web` from this environment (or install
it into the environment that runs Orchestra: `uv tool install ~/orchestra_2 --with ~/orchestra-outbox`).

Only someone logged in to Orchestra's web UI approves. The Outbox page and its API are behind
`orchestra web`'s login (the project's web token: open the link `orchestra web` prints, or paste
`orchestra web token` into its login page), and an approval or rejection carried by the browser's
session must come from Orchestra's own pages: a form or a script on another site gets `403`. A
program that approves for you would send `Authorization: Bearer <web token>`; nothing in the
outbox does.

## Configure

In the project's `orchestra.toml`:

```toml
[outbox]
owner = "person.danroblewis"          # the source your approvals publish as

[outbox.slack]                        # Slack's connection; the token is a file, never here

[outbox.kinds."review.request"]
sender = "slack.message"
template = "RR: $link cc: $reviewers"
```

Then put the token where the outbox looks:

```bash
mkdir -p ~/.config/orchestra-outbox
printf %s 'xoxp-...' > ~/.config/orchestra-outbox/slack-token
chmod 600 ~/.config/orchestra-outbox/slack-token
```

| System | Token | Scopes / rights |
|---|---|---|
| Slack | a user token (`xoxp-`), `~/.config/orchestra-outbox/slack-token` | `chat:write`; `channels:history`, `groups:history`, `im:history`, `mpim:history` (to check a send whose answer was lost) |
| GitHub | `~/.config/orchestra-outbox/github-token`, else your `gh` login | write issues and pull requests |
| Jira Data Center | a personal access token, `~/.config/orchestra-outbox/jira-token` | comment, transition, create in the projects you use |

Check a configuration before committing it (the Supervisor's tools call the same):

```bash
uv run orchestra-outbox check --repo ~/work            # exit 1 and the reasons when it is wrong
uv run orchestra-outbox check --file proposed.toml --json
```

### Kinds

| `sender` | Target | Content |
|---|---|---|
| `slack.message` | `{"channel": "C…"}` | the message |
| `slack.reply` | `{"channel": "C…", "thread_ts": "…"}` | the reply |
| `github.pr_comment` | `{"repo": "owner/name", "number": 7}` | the comment |
| `github.label` | same | one label name |
| `jira.comment` | `{"issue": "OPS-12"}` | the comment |
| `jira.transition` | `{"issue": "OPS-12"}` | the transition's name or id |
| `jira.create` | `{}` or `{"project": "OPS"}` | the description (`$content` in the kind's `payload`) |
| `script` | any object; `target = [...]` lists the keys required | whatever the script reads |

Per kind: `template`, `approval` (`"always"`, the default, or `"auto"`), `limit` and `per` (at most
`limit` sends per recipient per `per`; an `auto` draft over it waits for you). SPEC §4 has every
key and an example of each kind.

### Scripts

For a workflow no built-in sender knows (`arc feature`, a company tool), make the kind's sender a
script:

```toml
[outbox.kinds."port.request"]
sender = "script"
command = ["scripts/outbox/arc-port.sh"]
target = ["repo", "sha", "branch"]
```

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

`outbox_drafts_waiting`, `outbox_oldest_draft_waiting_seconds`, and counters of drafts created,
approved (by whom, edited or not), rejected, sent and failed, per kind, on `/metrics`, stored by
the instrument `outbox.outbox`.

## Limits

- Fakes, not real APIs, test the senders (`tests/fake_*.py`, mirroring the bridges' fakes). No
  opt-in contract test exists yet.
- A script is run again after a timeout or exit 75; only the script can make that safe.
- A Jira transition whose answer was lost counts as made when the issue no longer offers it.
- Autonomy is never earned automatically: the page shows the unedited approvals in a row, and you
  decide.
