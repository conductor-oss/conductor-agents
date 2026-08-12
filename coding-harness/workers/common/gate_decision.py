"""Shared logic for normalizing a human/agent WAIT-gate decision.

Three workflows (``issue_to_pr``, ``address_pr``, ``pr_review``) each read a
WAIT task's payload and reduce it to one canonical ``{action, feedback, ...}``
shape. The three JSON_JQ_TRANSFORM expressions that used to do this were
byte-for-byte identical apart from which extra field they carried (title/body,
body, or review) and whether ``investigate`` was a legal action. This module
is that one shared decision, plus the review-comment summarizer ``pr_review``
needs in two places.
"""

from __future__ import annotations


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _blank(value: object) -> bool:
    return not _text(value).strip()


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def resolve_gate_decision(gate: object, *, can_investigate: bool = False) -> dict:
    """Reduce a WAIT gate's payload to one action, independent of its shape.

    ``revise`` and ``investigate`` both require non-blank feedback -- an empty
    rationale is treated as though nothing had been requested, so a caller
    cannot silently trigger a repair loop with no instruction to act on.
    ``investigate`` is only legal when the caller says the budget is not spent.
    """
    gate = _mapping(gate)
    feedback = str(gate.get("feedback") or "")
    requested = gate.get("action")
    if not isinstance(requested, str) or not requested:
        requested = "approve" if gate.get("approved") is True else "unknown"
    has_feedback = not _blank(feedback)
    if requested == "approve" and gate.get("approved") is True:
        action = "approve"
    elif requested == "investigate" and can_investigate and has_feedback:
        action = "investigate"
    elif requested == "revise" and has_feedback:
        action = "revise"
    elif requested == "stop":
        action = "stop"
    else:
        action = "unknown"
    return {"action": action, "feedback": feedback, "gate": gate}


def _is_blocking(comment: object) -> bool:
    entry = _mapping(comment)
    return (entry.get("severity") == "blocking"
            and isinstance(entry.get("path"), str) and bool(entry.get("path"))
            and isinstance(entry.get("line"), (int, float)) and not isinstance(entry.get("line"), bool)
            and isinstance(entry.get("body"), str) and not _blank(entry.get("body")))


def summarize_review(review: object) -> dict:
    """Reduce a structured review to blocking comments plus a verdict.

    A clean review is exactly ``LGTM`` / ``approve`` with no comments, per the
    PR review policy; anything with at least one blocking, well-formed comment
    requests changes.
    """
    comments = [comment for comment in _listed(_mapping(review).get("comments"))
               if _is_blocking(comment)]
    clean = not comments
    return {"summary": "LGTM" if clean else "Changes requested.",
            "verdict": "approve" if clean else "request_changes",
            "comments": comments}
