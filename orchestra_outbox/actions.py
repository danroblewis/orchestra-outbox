"""The sensitive actions, as the owner decided them on 2026-10-27 (SPEC.md §6.6, §12):

- **pushing and opening a pull request** are allowed, as kinds like any other (approval ``always``
  unless the kind says ``auto``);
- **approving a pull request is forbidden.** No kind may do it, whatever its approval: a script
  that declares it, a script whose command names a known way to do it, and a draft whose content
  is a known approval (a review comment command, an approval label) are refused at the check, at
  drafting, at the owner's edit and again at sending. The GitHub sender also refuses to call the
  API's review-approval endpoints at all (``senders/github.py``);
- **a merge action** (merging a pull request, or applying one of ``[outbox] merge_labels``, e.g.
  SAIL) needs the owner's approval the first time for each pull request, whatever the kind's
  approval; later ones on the same pull request follow the kind's. The record keeps, per pull
  request, the first merge action the owner approved (``store.py``, ``merges``).

A script's ``action`` declares what it does (one of ``SCRIPT_ACTIONS``), because a script cannot be
read for intent; the check also scans its command line and, at sending, the script file itself for
the known ways to approve, so a declaration of ``other`` does not hide one."""

from __future__ import annotations

import re
from pathlib import Path

GIT_PUSH, PR_OPEN, PR_MERGE, PR_APPROVE, OTHER = "git.push", "github.pr_open", "github.pr_merge", "github.pr_approve", "other"
SCRIPT_ACTIONS = (OTHER, GIT_PUSH, PR_OPEN, PR_MERGE, PR_APPROVE)
FORBIDDEN = (PR_APPROVE,)

# The known ways to approve a pull request: GitHub's CLI and REST API (a review with the event
# APPROVE: POST /repos/{o}/{r}/pulls/{n}/reviews, or submitting a pending one: .../reviews/{id}/events),
# its GraphQL (addPullRequestReview / submitPullRequestReview with APPROVE), and the comment commands
# and labels that approve in Prow-style bots.
_APPROVE_IN_CODE = (
    re.compile(r"\bgh\s+pr\s+review\b[^\n]*(--approve\b|\s-a\b)"),
    re.compile(r"/pulls/[^/\s\"']+/reviews(/[^/\s\"']+/events)?\b[^\n]*\bAPPROVE\b"),
    re.compile(r"\bAPPROVE\b[^\n]*/pulls/[^/\s\"']+/reviews\b"),
    re.compile(r"\b(addPullRequestReview|submitPullRequestReview)\b[^\n]*\bAPPROVE\b"),
    re.compile(r"[\"']?event[\"']?\s*[:=]\s*[\"']?APPROVE\b"),
)
_APPROVE_COMMENT = re.compile(r"^\s*/(approve|lgtm)\b(?!\s+cancel)", re.IGNORECASE | re.MULTILINE)
APPROVAL_LABELS = ("approved", "lgtm")


def approves_in_code(text: str) -> bool:
    """Whether a command line or a script's text names a known way to approve a pull request."""
    return any(p.search(text) for p in _APPROVE_IN_CODE)


def script_approves(command: tuple[str, ...], path: str | None) -> bool:
    """A script kind's command line, and the script file when it can be read, scanned for approval."""
    if approves_in_code(" ".join(command)):
        return True
    if path:
        try:
            p = Path(path)
            if p.is_file() and p.stat().st_size <= 1_000_000:
                return approves_in_code(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return False
    return False


def content_approves(sender: str, content: str) -> str | None:
    """Why a draft's content would approve a pull request, or None: a label that approves, or a
    comment carrying an approval command."""
    if sender == "github.label" and content.strip().lower() in APPROVAL_LABELS:
        return f"the label {content.strip()!r} approves a pull request"
    if sender == "github.pr_comment" and _APPROVE_COMMENT.search(content):
        return "a line starting /approve or /lgtm approves a pull request"
    return None


def merge_pr(kind, target: dict, content: str, merge_labels: tuple[str, ...]) -> str | None:
    """The pull request (``owner/repo#n``) a draft would merge, or None when it is not a merge action."""
    is_merge = (kind.sender == "github.label" and content.strip().lower() in {m.lower() for m in merge_labels}) or (
        kind.sender == "script" and kind.action == PR_MERGE)
    if not is_merge:
        return None
    return f"{target['repo']}#{target['number']}"
