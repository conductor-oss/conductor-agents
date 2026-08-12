"""Pure decision logic for code_subtask's delivery outcome."""

from __future__ import annotations


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _blank(value: object) -> bool:
    return not _text(value).strip()


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def resolve_delivery_outcome(*, planned_paths: object, commit: object, audit: object,
                             agent: object, tested: object, test_state: object,
                             tests_passed: object) -> dict:
    """Compare what was planned against what actually got staged.

    Never trusts the agent's prose report: a subtask only "passed" when every
    planned path is staged and nothing was rejected.
    """
    commit, audit, agent = _mapping(commit), _mapping(audit), _mapping(agent)
    planned = sorted({path for path in _listed(planned_paths)
                      if isinstance(path, str) and not _blank(path)})
    staged = sorted(set(_listed(commit.get("stagedPaths")))) if isinstance(commit.get("stagedPaths"), list) else []
    rejected = sorted(set(_listed(commit.get("rejectedPaths")))) if isinstance(commit.get("rejectedPaths"), list) else []
    missing = [path for path in planned if path not in set(staged)]
    passed = not missing and not rejected

    if passed:
        state = "passed"
    elif audit.get("auditAvailable") is False:
        state = "audit_unavailable"
    elif agent.get("agentCompleted") is not True and not staged:
        state = "implementation_unavailable"
    else:
        state = "incomplete_delivery"

    tested_text = _text(tested)
    if not _blank(tested_text):
        resolved_commit = tested_text
    else:
        resolved_commit = commit.get("commit") or audit.get("head") or ""

    return {
        "state": state, "passed": passed, "plannedPaths": planned, "stagedPaths": staged,
        "missingPaths": missing, "rejectedPaths": rejected,
        "actualPaths": _listed(audit.get("actualPaths")),
        "commandFailures": _listed(audit.get("commandFailures")),
        "sandboxDenials": _listed(audit.get("sandboxDenials")),
        "agentStatus": agent.get("status") or "unknown",
        "agentCompleted": agent.get("agentCompleted") is True,
        "commit": resolved_commit,
        "testCycleState": test_state if isinstance(test_state, str) else "not_run",
        "testsPassed": tests_passed is True,
        "noOp": commit.get("noOp") is True,
    }
