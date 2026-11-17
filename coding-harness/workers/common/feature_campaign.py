"""Pure decision logic for feature_campaign."""

from __future__ import annotations

import re


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _blank(value: object) -> bool:
    return not _text(value).strip()


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def is_implementation_done(*, outcome: object, remaining: object) -> bool:
    """The implementation loop is done when nothing is left, or a terminal outcome landed."""
    if isinstance(remaining, list) and not remaining:
        return True
    return isinstance(outcome, str) and bool(outcome) and outcome != "running"


_ISSUE_PREAMBLE = re.compile(r"^\s*Resolve GitHub issue #\d+:.*$")


def _without_issue_preamble(value: object) -> str:
    """Drop the synthetic ``Resolve GitHub issue #n: <title>`` opening line.

    An issue-driven campaign's effective instruction is that preamble followed
    by the issue's real content. The description policy keeps only the first
    prose block, so leaving the preamble in place makes the published summary a
    restatement of the PR title and discards everything the issue actually
    said.
    """
    text = _text(value)
    lines = text.splitlines()
    if not lines or not _ISSUE_PREAMBLE.match(lines[0]):
        return text.strip()
    remainder = "\n".join(lines[1:]).strip()
    return remainder or text.strip()


def _first_line(value: object, limit: int = 120) -> str:
    """The first non-empty, non-heading line of a block of prose."""
    for raw in _text(value).splitlines():
        line = raw.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= limit else line[:limit - 1].rsplit(" ", 1)[0] + "\u2026"
    return ""


def resolve_campaign_pr_description(*, requested_title: object, requested_body: object,
                                    issue_title: object, instruction: object) -> dict:
    """Compose the PR title and body a campaign publishes.

    ``prTitle``/``prBody`` are optional inputs, and a campaign driven by an issue
    or a bare instruction supplies neither.  Without a fallback the draft carried
    no prose at all, so the published description compacted down to whatever the
    auto-appended issue link happened to be -- the ``falls back to instruction
    when blank`` behaviour the input schema documents was never implemented.

    The issue link is deliberately *not* concatenated here: ``pr_description``
    keeps only the first prose block, so a trailing ``Closes #n`` added upstream
    is dropped.  ``pr_create`` appends it from ``issueNumber`` instead.
    """
    title = _text(requested_title).strip() or _first_line(issue_title) or _first_line(instruction)
    body = _text(requested_body).strip() or _without_issue_preamble(instruction)
    return {"prTitle": title, "prBody": body, "prSummary": body}


def resolve_campaign_publication_plan(*, outcome: object, requested_draft: object,
                                      test_state: object, tests_passed: object,
                                      tested: object, committed: object,
                                      requested_title: object = "",
                                      requested_body: object = "",
                                      issue_title: object = "",
                                      instruction: object = "") -> dict:
    """Decide the publish head and draft state after the final test cycle."""
    tested_text = _text(tested)
    head = tested_text if not _blank(tested_text) else committed
    description = resolve_campaign_pr_description(
        requested_title=requested_title, requested_body=requested_body,
        issue_title=issue_title, instruction=instruction)
    return {
        **description,
        "draft": outcome != "verified" or requested_draft is True or tests_passed is not True,
        "outcome": outcome if isinstance(outcome, str) and outcome else "incomplete",
        "head": head,
        # A draft to approve exists only when something was committed and the campaign ran to its end:
        # a campaign stopped before implementation has no PR to park a human gate on.
        "publish": "yes" if not _blank(_text(head)) and outcome in ("verified", "incomplete") else "no",
        "testCycleState": test_state if isinstance(test_state, str) else "not_run",
        "testsPassed": tests_passed is True,
    }
