# The Outbox: an operator's guide

For the AI operator (the Supervisor) who sets up the `outbox` plugin in a user's project. It is
what you need to choose settings well. It is not a procedure. The full contract is the plugin's
SPEC §4. Every key named here exists, and an unknown key is refused.

## What it is, and whose choices these are

The Outbox is the one way anything is said or done **as the owner** in another system: a Slack
message, a GitHub comment, label or new pull request, a Jira comment, transition or issue, or
whatever a script does. Machines write **drafts**. The owner approves (perhaps after editing) or
rejects each one on the Outbox page. The outbox machine sends what was approved, once.

Most of its settings decide **what is said in the owner's name, to whom, and without asking
them**. Those choices belong to the owner. You propose them, explain the trade-off and ask. You
never decide them yourself:

- `approval = "auto"` on any kind (anything that sends without the owner seeing it);
- which kinds exist, and their channels, repositories, projects and templates (what the owner says);
- `ping`, `limit`, `per` and `patience` on a kind that reaches people;
- `merge_labels`, and any script whose `action` is `git.push`, `github.pr_open` or `github.pr_merge`;
- the owner's identity (`owner`), accounts and tokens.

The safe default is that **everything waits for approval**. That is also the plugin's own
default: a kind with no `approval` key is `"always"`. Never write `"auto"` because a draft "looks
routine", and never as a fix for a backlog. The Outbox page shows each kind's run of unedited
approvals, and after 10 in a row it says "could be automatic: ask the Supervisor". That is
evidence to show the owner, not permission. Wait until the owner asks.

What you can decide yourself is the plumbing: which file to write, a system's connection table, a
port, kind names, and fixing a check error.

## Where settings go

- **The private overlay** (`orchestra.local.toml` in the project's state directory, `target =
  "overlay"` in `project.config_edit`) is where the owner's kinds, channel ids, templates, payloads,
  `owner` and `merge_labels` belong. It is never committed. Default to it.
- **The repository's `orchestra.toml`** (`target = "repo"`) is shared with the team. Put only what
  the whole team agrees on there, such as an empty `[outbox]` and the system tables.
- **Never in either file:** tokens, API passwords, or anything secret. `[outbox.<system>] token`
  is refused. Tokens live in `~/.config/orchestra/` (see "Credentials").

How the overlay merges matters. Tables merge key by key, and the overlay wins. So the overlay can
change one key of a kind the repository defines (its `approval`, for example), but it cannot
delete that kind. `merge_labels`, `command`, `target` and other arrays are replaced whole, not
appended to. `[overlay] secret = ["outbox.kinds"]` in the overlay hides values from views. It
hides them; it does not make them a place for credentials.

Having an `[outbox]` table at all switches the plugin on. That table makes `orchestra run` start
the outbox machine (`outbox.sender`) and scrape its `/metrics`. Without it the plugin shows its
page and does nothing.

## Checking a change

`project.config_edit` validates the merged configuration with the plugin's check,
`orchestra_outbox.config.problems(raw, repo_root)`. It returns `(settings, errors)`. Each error is
one sentence that names the key, and every error is reported at once. Errors are what the machine
would refuse to start with. If the running machine rereads a file that fails, it logs the error and
keeps the last good version, so a bad edit silently changes nothing.

`orchestra_outbox.check.check(raw, repo_root)` returns the same errors plus **warnings**, which do
not block the edit but predict a failed send:

- a script that is not a file, or is not executable;
- a script file that names a way to approve a pull request (every send of it will be refused);
- an `auto` `github.label` kind with no `merge_labels`;
- an owner left at the default `"owner"`, or one that is not `person.<key>`;
- a `token_file` that others can read.

Read the warnings and pass them on to the owner. The command-line form for humans is
`orchestra outbox check --repo <repo> [--json]`. `orchestra outbox example` prints the owner's
five starting kinds.

## `[outbox]`

```toml
[outbox]
owner = "person.daniel"   # the person id approvals publish as; never "outbox"
people = "people.toml"    # the identity map, relative to the repository; default [bridges] people
merge_labels = ["SAIL"]   # labels that merge a PR when added
host = "127.0.0.1"        # loopback only: the drafting API and /metrics
port = 9467               # 0 = any free port (in <state>/outbox/endpoint.json), and then no metrics scrape
```

- `owner`: found in this order. First `[outbox] owner`. Then `owner` in
  `~/.config/orchestra/accounts.toml`. Then the one identity-map entry (not `bot = true`) that
  lists one of the owner's accounts. Otherwise `"owner"`, which produces a warning. Prefer
  `accounts.toml`'s `owner`: it is shared with the bridges, so the owner's approvals and their own
  Slack replies are one machine on the bus. Set `[outbox] owner` only to override it. Ask the
  owner for their person id; do not invent one.
- `merge_labels`: ask the owner which label merges in their repositories (a bot such as SAIL that
  merges on a label). Adding one of these labels is a **merge action**. The first merge action on
  each pull request always waits for the owner, even on an `auto` kind. An empty list means a
  label kind can merge with no such guard.
- `host` and `port`: leave them alone unless 9467 is taken. `port = 0` costs the metrics graphs,
  because the instrument needs a fixed address.

## Systems: `[outbox.slack]`, `[outbox.github]`, `[outbox.jira]`

```toml
[outbox.slack]                          # present = configured; empty is normal
[outbox.github]
[outbox.jira]
api = "https://jira.example.com"        # Jira has no default: here, or [jira] api in accounts.toml
```

A system table only enables that system's built-in senders, and a kind that uses a system needs its
table. The only keys are `api` and `token_file`.

- `api` defaults to `accounts.toml`'s `[<system>] api`, then to Slack's `https://slack.com/api` or
  GitHub's `https://api.github.com`. Jira has no default. It must be `https`; Slack's must be on
  `slack.com`; plain `http` is accepted only for a loopback fake. For GitHub Enterprise, set the
  host's API URL. Prefer putting a Jira URL in `accounts.toml` so the bridges share it.
- `token_file` overrides the shared token path. It must be outside the repository and `chmod 600`.
  You will rarely need it.

## Kinds: `[outbox.kinds."<name>"]`

A kind is one thing the owner is willing to have drafted. Its name is lowercase dotted words
(`review.request`). Drafters name it, so choose stable names and do not rename a kind that machines
already use.

```toml
[outbox.kinds."review.request"]
sender = "slack.message"
template = "RR: $link cc: $reviewers"
approval = "always"          # write it out; loosening it later is then a visible edit
ping = true                  # reaches people: drafts must name recipients
limit = 1                    # at most 1 send per recipient ...
per = "24h"                  # ... per day (at least 60s)
patience = "48h"             # default 2 × per; at least 60s
```

| `sender` | Draft's target | Content |
|---|---|---|
| `slack.message` | `{channel = "C…"}` | the message |
| `slack.reply` | `{channel, thread_ts}` | the reply |
| `github.pr_comment` | `{repo = "o/r", number}` | the comment (a line starting `/approve` or `/lgtm` is refused) |
| `github.label` | `{repo, number}` | one label name (`approved` and `lgtm` are refused) |
| `github.pr_open` | `{repo, head, base}`, plus a `title` field | the PR body |
| `jira.comment` | `{issue = "OPS-12"}` | the comment |
| `jira.transition` | `{issue}` | the transition's name or id |
| `jira.create` | `{}` or `{project = "OPS"}` | `$content` in the payload |
| `script` | any object; `target` lists required keys | whatever the script reads |

The drafter supplies the target, not the kind. A kind cannot pin a channel; a template can only
shape the text. If the owner wants a fixed channel, the drafting machine's own configuration is
where that goes.

**`template`**: `$name` or `${name}` from the draft's `fields`, `$content` for the drafter's text,
and `$$` for a dollar sign. No expressions. A kind without a template needs the draft to carry
`content`. A template with no placeholders fixes the content (`template = "Done"` on a transition,
`"SAIL"` on a label). The owner can still edit the draft before approving. A missing field refuses
the draft at drafting time, not at check time. So when you write a template, tell the drafter's
owner which fields it needs.

**`approval`**: `"always"` (the default) or `"auto"`. See the first section. `auto` still obeys
`limit`, and still holds the first merge action per PR for the owner.

**`ping`, `limit`, `per`, `patience` (rate limits).**
- `limit` counts sends of this kind **per recipient** that began in the last `per`, whoever
  approved them. A draft over the limit is never dropped and never sent back to the owner. It waits
  for the window, in approval order.
- **Recipients** are what a limit counts. Without `ping` they default to the target (the channel,
  `owner/repo#n`, the issue key, or `"script"` for every script draft), so `limit = 1` on a
  `slack.message` kind means one message per channel per window. That is rarely what the owner
  means. When the limit is about *people* (pings, nudges, review requests), set `ping = true`.
  Drafts must then name `recipients` (Slack member ids, for example), and the limit counts per
  person. A ping kind with no recipients is refused at drafting.
- `limit = 0` means nothing of this kind is ever sent. Every draft waits and then becomes
  unresolvable. Do not use it as an off switch; remove the kind, or leave it `always` and let the
  owner reject drafts.
- `patience` is how long a draft may wait before the limit is declared unresolvable. It defaults to
  twice `per`, or one hour without a `limit` (which is how an API's own rate limit is judged). A
  limit is also unresolvable when drafts arrive faster than the limit lets them out (`outpaced`).
  Either way, the drafting machine publishes `outbox.rate_limit.unresolvable`. The fix is the
  owner's to choose: reject drafts, raise `limit` or `per`, lengthen `patience`, or make the
  drafter draft less. Never turn on `auto` to clear a backlog. It changes who approves, not how
  fast things are sent.
- When proposing a limit, think in terms of the recipient's experience: "at most one nudge per
  reviewer per day" is `ping = true`, `limit = 1`, `per = "24h"`.

**`payload`** (`jira.create` only, and required there) holds the new issue's `fields`. It must have
`project`, `issuetype` and `summary`. Strings are rendered at send time from the fields and
`$content` (the approved, perhaps edited, text). A string that is exactly one placeholder keeps the
field's type, so `"$sprint"` stays a number. A draft's `target.project` overrides `project`. Copy
the owner's real issue template (types, labels, components, custom field ids). Ask; do not guess.

```toml
[outbox.kinds."jira.task"]
sender = "jira.create"
template = "$content"
[outbox.kinds."jira.task".payload]
project = { key = "OPS" }
issuetype = { name = "Task" }
summary = "$summary"
description = "$content"
customfield_10007 = "$sprint"   # a number stays a number
```

**`command`, `action`, `target`, `timeout`** (`script` only) are for anything no built-in sender
does: a push, a merge, a company tool, a plain HTTP call or webhook. There is no generic HTTP
sender, so those go through a script.

```toml
[outbox.kinds."port.request"]
sender = "script"
command = ["scripts/outbox/arc-port.sh"]   # a first argument with "/" is relative to the repository; otherwise PATH
action = "other"                           # other | git.push | github.pr_open | github.pr_merge
target = ["repo", "sha", "branch"]         # keys a draft's target must carry
timeout = "10m"                            # 1s..1h, default 120s
```

- `action` declares what the script does. It is required, and it is how the Outbox knows a merge
  (`github.pr_merge` adds `repo` and `number` to `target` and gets the first-merge guard).
  Declare it honestly: a merge script declared `other` loses that guard. `github.pr_approve` is
  refused, and so is a command or script that names a known way to approve a pull request.
- The script receives the approved draft as JSON on stdin (`id`, `kind`, `target`, `recipients`,
  `content`, `fields`, ...), plus `OUTBOX_DRAFT_ID`, `OUTBOX_KIND` and `OUTBOX_ATTEMPT`. It runs in
  the repository root. Exit 0 means sent. Exit 75, or a timeout, means "run again later". Anything
  else means failed for good. **It must be idempotent on `OUTBOX_DRAFT_ID`**, because it may run
  twice. Scripts bring their own credentials.
- You may find or point to a script the owner already has in their codebase. Writing a new one,
  or changing what it does, is code the owner reviews. Whether a script that pushes or merges may
  exist at all is the owner's decision.

## Sensitive actions (fixed; no setting changes them)

- **Approving a pull request is impossible.** It is refused at the check, at drafting, at the
  owner's edit and at sending.
- **Merge actions** are a `merge_labels` label, or a script with `action = "github.pr_merge"`. The
  first one on each PR waits for the owner, even on an `auto` kind. Later ones on that PR follow the
  kind's `approval`. If the owner rejects the first, the next attempt counts as the first again.
- **Pushing and opening PRs** (`github.pr_open`, a script with `git.push` or `github.pr_open`) are
  ordinary kinds. Propose `always` for them.

## Credentials and accounts (outside every repository)

```toml
# ~/.config/orchestra/accounts.toml (0600; the directory 0700; $ORCHESTRA_ACCOUNTS moves it). No secrets.
owner = "person.daniel"
[slack]
account = "U012AB3CD"           # the owner's member id
[github]
account = "danroblewis"
[jira]
api = "https://jira.example.com"
account = "dlewis"
```

Tokens are one file each, `~/.config/orchestra/tokens/<system>-token`, `chmod 600`, not empty, and
outside the repository.

| System | Token | Rights |
|---|---|---|
| Slack | a **user** token `xoxp-` (a bot token would post as the bot) | `chat:write`, plus `channels:history`, `groups:history`, `im:history`, `mpim:history` (to find a send whose answer was lost) |
| GitHub | `github-token`, else the `gh` CLI's login | write issues and pull requests; contents, to open one |
| Jira Data Center | a personal access token | comment, transition and create in the projects used |

The Outbox looks for each system's token in this order:

1. the `<SYSTEM>_OUTBOX_TOKEN` environment variable, or the file `<SYSTEM>_OUTBOX_TOKEN_FILE` names
   (these are for tests);
2. `token_file`;
3. the shared file;
4. the legacy `~/.config/orchestra-outbox/<system>-token`;
5. for GitHub only, `gh auth token`.

A token is read when a send first needs it. So a missing token does not fail the check; it fails
the send (`no_credentials`, retried up to 8 tries, then `failed`). A read-only token shared with the bridges (Jira's,
often) lets the Outbox read but not write.

These files are the owner's. You may tell the owner exactly which file, scopes and permissions are
needed, and check that a path exists. You never create, read or move a token, and you never paste
an account id the owner has not given you.

## Pitfalls

- A kind for a system whose table is absent is an error. Add `[outbox.<system>]` too.
- The owner's starting kinds (`orchestra outbox example`) include `[outbox.jira]`, which needs a
  Jira `api` from somewhere, and `merge_labels = ["SAIL"]`, which is wrong for an owner without
  SAIL. Install it into the overlay and adjust it, then check.
- A typo in a key is an error, not a default. A wrong *value* that parses (a wrong channel id or
  issue key) is only discovered at drafting or sending.
- The `per` window counts sends that *began*, whoever approved them. Sends of an `auto` kind and
  sends the owner approved share one budget.
- Content never reaches the bus or the metrics. To debug a send, read the draft on the Outbox page
  (`error` is a code, `output` is a script's last 4 KB). Never add logging that echoes content.
- The machine rereads `[outbox]` when the file changes, and the web page rereads it on each
  request, so no restart is needed. If a kind is removed while drafts of it are waiting, those
  drafts fail with `kind_not_configured` when they are approved. Let them drain or be rejected
  first, and do not remove a kind that a drafter still names.
