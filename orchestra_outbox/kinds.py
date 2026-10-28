"""The senders a kind can name, and what each one's draft must carry.

A **kind** is configured, never coded (``config.py``): a name, the sender that sends it, perhaps a
template, its approval and its limits. A **sender** is one of the built-in ones below, each of one
system, or ``script``: a command that receives the approved draft as JSON on stdin.

For each sender this module knows the target it needs (validated, and kept to its known keys), the
content it takes, and whom a draft reaches when the drafter names nobody."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass


class DraftError(ValueError):
    """A draft (or an edit of one) that cannot be taken: an unknown kind, a bad target, bad content,
    a field its template needs and it lacks."""


_CHANNEL = re.compile(r"^[CDG][A-Z0-9]{2,}$")          # a Slack conversation id
_TS = re.compile(r"^\d{9,11}\.\d{1,6}$")                # a Slack message ts
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ISSUE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")           # a Jira issue key
_PROJECT = re.compile(r"^[A-Z][A-Z0-9_]+$")
_BRANCH = re.compile(r"^([A-Za-z0-9_.-]+:)?[A-Za-z0-9_.][A-Za-z0-9_./-]{0,254}$")   # a branch, perhaps owner:branch
MAX_TARGET = 4096


def _slack_channel(t: dict) -> dict:
    ch = t.get("channel")
    if not isinstance(ch, str) or not _CHANNEL.match(ch):
        raise DraftError("target.channel must be a Slack conversation id (C…, G… or D…)")
    return {"channel": ch}


def _slack_thread(t: dict) -> dict:
    out = _slack_channel(t)
    ts = t.get("thread_ts")
    if not isinstance(ts, str) or not _TS.match(ts):
        raise DraftError("target.thread_ts must be the ts of the thread's first message")
    return {**out, "thread_ts": ts}


def _github_pr(t: dict) -> dict:
    repo, n = t.get("repo"), t.get("number")
    if not isinstance(repo, str) or not _REPO.match(repo):
        raise DraftError("target.repo must be owner/name")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise DraftError("target.number must be a pull request's number")
    return {"repo": repo, "number": n}


def _github_new_pr(t: dict) -> dict:
    repo = t.get("repo")
    if not isinstance(repo, str) or not _REPO.match(repo):
        raise DraftError("target.repo must be owner/name")
    out = {"repo": repo}
    for k in ("head", "base"):
        v = t.get(k)
        if not isinstance(v, str) or not _BRANCH.match(v) or ".." in v:
            raise DraftError(f"target.{k} must be a branch name" + (" (or owner:branch)" if k == "head" else ""))
        out[k] = v
    return out


def _github_repo(t: dict) -> dict:
    repo = t.get("repo")
    if not isinstance(repo, str) or not _REPO.match(repo):
        raise DraftError("target.repo must be owner/name")
    return {"repo": repo}


def _jira_issue(t: dict) -> dict:
    key = t.get("issue")
    if not isinstance(key, str) or not _ISSUE.match(key):
        raise DraftError("target.issue must be a Jira issue key (OPS-123)")
    return {"issue": key}


def _jira_project(t: dict) -> dict:
    p = t.get("project")
    if p is None:
        return {}
    if not isinstance(p, str) or not _PROJECT.match(p):
        raise DraftError("target.project, when given, must be a Jira project key (OPS)")
    return {"project": p}


def _any(t: dict) -> dict:
    """A script's target: any JSON object of ids, at most 4 KB (the kind may require keys)."""
    if len(json.dumps(t)) > MAX_TARGET:
        raise DraftError(f"target is larger than {MAX_TARGET} bytes")
    return t


def _text(limit: int) -> Callable[[str], str]:
    def check(content: str) -> str:
        if not content.strip():
            raise DraftError("content is empty")
        if len(content) > limit:
            raise DraftError(f"content is longer than {limit} characters")
        return content
    return check


def _name(what: str) -> Callable[[str], str]:
    def check(content: str) -> str:
        name = content.strip()
        if not name or len(name) > 100 or "\n" in name or "," in name:
            raise DraftError(f"the content of a {what} draft is one {what} name: one line, at most 100 characters, "
                             "no comma")
        return name
    return check


def _pr(t: dict) -> str:
    return f"{t['repo']}#{t['number']}"


@dataclass(frozen=True)
class Spec:
    sender: str
    system: str                                # whose connection ([outbox.<system>]) it uses
    target: Callable[[dict], dict]
    content: Callable[[str], str]
    recipient: Callable[[dict], str]           # who it reaches, when the drafter names nobody
    idempotent: bool = False                   # sending it twice changes nothing
    payload: bool = False                      # takes a [payload] table (jira.create: the issue's fields)
    action: str = ""                           # what it does (actions.py); a script declares its own
    needs: tuple[str, ...] = ()                # fields a draft must carry (github.pr_open: its title)
    owner_only: bool = False                   # the owner approves every one: a kind of it may not be "auto"


SPECS: dict[str, Spec] = {s.sender: s for s in (
    Spec("slack.message", "slack", _slack_channel, _text(40_000), lambda t: t["channel"], action="slack.post"),
    Spec("slack.reply", "slack", _slack_thread, _text(40_000), lambda t: t["channel"], action="slack.post"),
    Spec("github.pr_comment", "github", _github_pr, _text(60_000), _pr, action="github.pr_comment"),
    Spec("github.label", "github", _github_pr, _name("label"), _pr, idempotent=True, action="github.pr_label"),
    Spec("github.pr_open", "github", _github_new_pr, _text(60_000), lambda t: t["repo"], action="github.pr_open",
         needs=("title",)),
    Spec("github.issue_create", "github", _github_repo, _text(60_000), lambda t: t["repo"],
         action="github.issue_create", needs=("title",), owner_only=True),
    Spec("jira.comment", "jira", _jira_issue, _text(32_000), lambda t: t["issue"], action="jira.comment"),
    Spec("jira.transition", "jira", _jira_issue, _name("transition"), lambda t: t["issue"], action="jira.transition"),
    Spec("jira.create", "jira", _jira_project, _text(32_000), lambda t: t.get("project", "jira"), payload=True,
         action="jira.create"),
    Spec("script", "script", _any, _text(100_000), lambda t: "script"),
)}

SYSTEMS = ("slack", "github", "jira")          # the systems whose connection is configured; scripts need none


def check(spec: Spec, target: object, content: object) -> tuple[dict, str]:
    if not isinstance(target, dict):
        raise DraftError("target must be an object")
    if not isinstance(content, str):
        raise DraftError("content must be a string")
    return spec.target(target), spec.content(content)
