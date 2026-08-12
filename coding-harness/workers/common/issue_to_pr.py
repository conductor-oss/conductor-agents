"""Pure decision logic for issue_to_pr, replacing several JSON_JQ_TRANSFORM tasks."""

from __future__ import annotations

_DELIVERY_STATES = {"passed", "incomplete_delivery", "merge_blocked",
                    "audit_unavailable", "implementation_unavailable"}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _blank(value: object) -> bool:
    return not _text(value).strip()


def _num(value: object) -> float | int:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def normalize_issue_delivery(*, child: object, publication_commit: object,
                             workspace_state: object, fallback_commit: object) -> dict:
    """Fold the code_parallel child's report into the delivery state issue_to_pr tracks."""
    child = _mapping(child)
    commit_report = _mapping(publication_commit)
    workspace = _mapping(workspace_state)

    child_state = child.get("deliveryOutcome")
    if child_state not in _DELIVERY_STATES:
        child_state = child.get("verificationState")
    if child_state not in _DELIVERY_STATES:
        child_state = "implementation_unavailable"
    parent_rejected = _listed(commit_report.get("rejectedPaths"))
    # A merge that reports success but still had rejected paths at the actual
    # commit step is not a complete delivery, whatever the child claimed.
    state = "incomplete_delivery" if child_state == "passed" and parent_rejected else child_state

    commit = commit_report.get("commit")
    if isinstance(commit, str) and not _blank(commit):
        resolved_commit = commit
    else:
        head = workspace.get("head")
        resolved_commit = head if isinstance(head, str) and not _blank(head) else fallback_commit

    rejected = list(dict.fromkeys(_listed(child.get("rejectedPaths")) + parent_rejected))
    return {
        "state": state, "commit": resolved_commit,
        "summary": child.get("proposalText") if isinstance(child.get("proposalText"), str)
                   else "Automated change.",
        "subtasks": _listed(child.get("subtasks")), "merged": _listed(child.get("merged")),
        "conflicts": _listed(child.get("conflicts")),
        "totalTokens": _num(child.get("totalTokens")), "totalCostUsd": _num(child.get("totalCostUsd")),
        "repairAttempts": _num(child.get("deliveryRepairAttempts")),
        "reports": _listed(child.get("deliveryReports")),
        "missingPaths": _listed(child.get("missingPaths")), "rejectedPaths": rejected,
        "commandFailures": _listed(child.get("commandFailures")),
        "sandboxDenials": _listed(child.get("sandboxDenials")),
    }


def normalize_verified_delivery(*, delivery: object, verification: object) -> dict:
    """Fold the post-merge test_cycle result into the delivery record."""
    delivery = _mapping(delivery)
    verification = _mapping(verification)
    repair_reports = _listed(verification.get("repairReports"))
    attempts = _num(verification.get("attempts"))
    test_failures = [
        {"command": command.get("argv") or [], "exitCode": command.get("exitCode"),
         "output": command.get("output") or ""}
        for command in _listed(_mapping(verification.get("verification")).get("commands"))
        if _num(command.get("exitCode")) != 0
    ]
    candidate = verification.get("candidateCommit")
    commit = candidate if isinstance(candidate, str) and not _blank(candidate) else delivery.get("commit")

    delivery_state = delivery.get("state")
    verification_state = verification.get("verificationState")
    if delivery_state != "passed":
        state = delivery_state
    elif verification_state == "passed":
        state = "passed"
    elif verification_state == "failed":
        state = "incomplete_delivery"
    else:
        state = "implementation_unavailable"

    return {**delivery, "state": state, "commit": commit,
            "repairAttempts": _num(delivery.get("repairAttempts")) + attempts,
            "reports": _listed(delivery.get("reports")) + repair_reports,
            "commandFailures": _listed(delivery.get("commandFailures")) + test_failures,
            "verification": verification}


def resolve_publication_plan(delivery_outcome: object, *, agent_authored_test: object = False) -> dict:
    """Decide whether the PR should open as a draft, and why.

    ``agent_authored_test`` forces draft even when delivery passed: a
    red/green check already proved the new test isn't vacuous, but a human
    should still see that no pre-existing test covered this change before
    the PR is treated as fully verified (defense in depth).
    """
    outcome = delivery_outcome if isinstance(delivery_outcome, str) and delivery_outcome \
        else "implementation_unavailable"
    authored = agent_authored_test is True
    passed = outcome == "passed" and not authored
    # "true" stays a string, matching the workflow's own variable default and
    # the value-param SWITCH convention used elsewhere in this harness.
    reason = "" if passed else (
        "agent-authored test" if authored and outcome == "passed" else f"delivery outcome: {outcome}"
    )
    return {"publish": "true", "draft": not passed, "reason": reason, "agentAuthoredTest": authored}


def normalize_publication_result(*, push: object, pr: object) -> dict:
    """Report whichever of push/PR creation actually ran, never both silently."""
    push, pr = _mapping(push), _mapping(pr)
    state = pr.get("publicationState")
    if not isinstance(state, str):
        state = push.get("publicationState")
    if not isinstance(state, str):
        state = "publication_unavailable"
    return {"publicationState": state, "pushed": push.get("pushed") is True,
            "number": pr.get("number") if isinstance(pr.get("number"), (int, float))
                      and not isinstance(pr.get("number"), bool) else 0,
            "url": pr.get("url") if isinstance(pr.get("url"), str) else ""}
