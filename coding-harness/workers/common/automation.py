"""Pure helpers for GitHub-backed automation state.

State is stored as versioned, hidden GitHub comments.  Parsing is deliberately
strict: malformed markers and markers written by an unexpected identity are ignored.
The pure functions live here so eligibility and retry behavior can be unit tested
without GitHub or Conductor.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


MARKER_VERSION = 1
MARKER_PREFIX = "<!-- conductor-automation:"
MARKER_RE = re.compile(r"<!-- conductor-automation:(\{.*?\}) -->", re.DOTALL)
TERMINAL_OUTCOMES = {"completed", "suppressed", "exhausted"}
ACTIVE_STATUSES = {"RUNNING", "PAUSED", "SCHEDULED", "IN_PROGRESS"}
RETRYABLE_STATUSES = {"FAILED", "TIMED_OUT", "TERMINATED", "FAILED_WITH_TERMINAL_ERROR"}


@dataclass(frozen=True)
class Marker:
    kind: str
    revision: str
    status: str
    attempt: int
    workflow_id: str = ""
    timestamp: str = ""
    author: str = ""
    comment_id: int = 0
    raw: dict | None = None


def encode_marker(data: dict) -> str:
    payload = {"v": MARKER_VERSION, **data}
    return f"{MARKER_PREFIX}{json.dumps(payload, sort_keys=True, separators=(',', ':'))} -->"


def parse_marker(body: str, *, author: str, trusted_author: str,
                 comment_id: int = 0) -> Marker | None:
    if not trusted_author or author.casefold() != trusted_author.casefold():
        return None
    match = MARKER_RE.search(body or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        if data.get("v") != MARKER_VERSION:
            return None
        kind = str(data["kind"])
        revision = str(data["revision"])
        status = str(data["status"]).lower()
        attempt = int(data.get("attempt") or 1)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not kind or not revision or attempt < 1:
        return None
    return Marker(kind, revision, status, attempt,
                  str(data.get("workflowId") or ""), str(data.get("timestamp") or ""),
                  author, int(comment_id or 0), data)


def trusted_markers(comments: Iterable[dict], trusted_author: str) -> list[Marker]:
    found = []
    for comment in comments:
        marker = parse_marker(
            str(comment.get("body") or ""),
            author=str((comment.get("user") or comment.get("author") or {}).get("login") or ""),
            trusted_author=trusted_author,
            comment_id=int(comment.get("id") or 0),
        )
        if marker:
            found.append(marker)
    return found


def _feedback_rows(*collections: Iterable[dict], excluded_author: str = "") -> list[dict]:
    """Normalize actionable feedback while excluding harness operational comments."""
    rows: list[dict] = []
    for collection in collections:
        for item in collection or []:
            body = " ".join(str(item.get("body") or "").split())
            author = str((item.get("user") or item.get("author") or {}).get("login") or "")
            if not body or MARKER_PREFIX in body:
                continue
            if excluded_author and author.casefold() == excluded_author.casefold() and \
                    "conductor-harness" in body:
                continue
            rows.append({
                "id": str(item.get("id") or item.get("databaseId") or ""),
                "updated": str(item.get("updated_at") or item.get("updatedAt") or ""),
                "author": author.casefold(),
                "state": str(item.get("state") or ""),
                "path": str(item.get("path") or ""),
                "line": item.get("line") or item.get("original_line") or 0,
                "body": body,
            })
    return rows


def feedback_fingerprint(*collections: Iterable[dict], excluded_author: str = "") -> str:
    """Stable fingerprint across review, inline, and conversation feedback.

    IDs, update timestamps and normalized bodies are included. Harness operational
    markers are excluded, but formal automated reviews remain actionable.
    """
    rows = _feedback_rows(*collections, excluded_author=excluded_author)
    encoded = json.dumps(sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)),
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def actionable_feedback_count(*collections: Iterable[dict], excluded_author: str = "") -> int:
    return len(_feedback_rows(*collections, excluded_author=excluded_author))


def issue_has_label(issue: dict, label: str = "conductor:auto") -> bool:
    labels = {str((item if isinstance(item, str) else item.get("name")) or "").casefold()
              for item in issue.get("labels") or []}
    return str(issue.get("state") or "OPEN").upper() == "OPEN" and not issue.get("isDraft") \
        and label.casefold() in labels


def issue_revision(issue: dict) -> str:
    """Revision unaffected by automation comments (which mutate issue.updated_at)."""
    labels = sorted(str((item if isinstance(item, str) else item.get("name")) or "").casefold()
                    for item in issue.get("labels") or [])
    payload = {"id": issue.get("id") or issue.get("number"),
               "title": issue.get("title") or "", "body": issue.get("body") or "",
               "labels": labels}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def linked_pr_exists(issue_number: int, prs: Iterable[dict]) -> bool:
    close_re = re.compile(rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#?{issue_number}\b", re.I)
    branch_re = re.compile(rf"(?:^|[-_/])issue[-_/]?{issue_number}(?:$|[-_/])", re.I)
    for pr in prs or []:
        state = str(pr.get("state") or "").upper()
        if state not in {"OPEN", "MERGED"}:
            continue
        text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        if close_re.search(text) or branch_re.search(str(pr.get("headRefName") or "")):
            return True
    return False


def revision_decision(markers: Iterable[Marker], revision: str, *, now_epoch: float,
                      workflow_status: dict[str, str] | None = None,
                      retry_after_seconds: int = 1800, max_attempts: int = 3) -> tuple[bool, str, int]:
    """Return (eligible, reason, next_attempt) for one revision."""
    relevant = [m for m in markers if m.revision == revision]
    if not relevant:
        return True, "new", 1
    # GitHub comment IDs are chronological. This matters for reset markers, whose
    # attempt intentionally returns to 1 after an exhausted attempt 3.
    relevant.sort(key=lambda m: (m.comment_id, m.attempt))
    latest = relevant[-1]
    if latest.status == "reset":
        return True, "reset", 1
    if latest.status in TERMINAL_OUTCOMES:
        return False, latest.status, latest.attempt
    # Concurrent claimants all leave a marker. If no later state marker exists,
    # the earliest claim for the latest attempt is the race winner whose dispatch
    # execution determines whether this revision is still active or failed.
    state = latest
    if latest.status == "claimed":
        attempt = max(m.attempt for m in relevant)
        claims = [m for m in relevant if m.status == "claimed" and m.attempt == attempt]
        if claims:
            state = min(claims, key=lambda m: m.comment_id)
    child_status = (workflow_status or {}).get(state.workflow_id, "").upper()
    if child_status == "SUPPRESSED":
        return False, "suppressed", state.attempt
    if child_status in ACTIVE_STATUSES:
        return False, "active", state.attempt
    if state.attempt >= max_attempts and (child_status in RETRYABLE_STATUSES or
                                          state.status == "failed"):
        return False, "exhausted", state.attempt
    if child_status in RETRYABLE_STATUSES and state.status != "failed":
        # The scanner records a fresh failure marker first. Backoff must begin when
        # the failure is discovered, not when a long-running child originally began.
        return False, "record_failure", state.attempt
    stamp = _parse_time(state.timestamp)
    if state.status == "failed":
        if stamp is not None and now_epoch - stamp < retry_after_seconds:
            return False, "retry_backoff", state.attempt
        return True, "retry", state.attempt + 1
    if state.status == "claimed" and not child_status:
        return False, "claimed", state.attempt
    return False, state.status or "claimed", state.attempt


def _parse_time(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
