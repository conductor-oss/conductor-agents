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
                             tests_passed: object, optional_paths: object = None) -> dict:
    """Compare what was planned against what actually got staged.

    Never trusts the agent's prose report: a subtask only "passed" when every
    planned path is staged and nothing was rejected. An ``optional_paths`` entry
    (a frozen acceptance test, writable only so a wrong assertion can be
    amended) may be left untouched without counting as missing.
    """
    commit, audit, agent = _mapping(commit), _mapping(audit), _mapping(agent)
    planned = sorted({path for path in _listed(planned_paths)
                      if isinstance(path, str) and not _blank(path)})
    optional = {path for path in _listed(optional_paths) if isinstance(path, str) and not _blank(path)}
    # Planned paths the audit found in HEAD's tree before the session: delivered by an earlier attempt
    # (a review re-dispatch changes only what the reviewer asked for), never missing.
    present = {path for path in _listed(audit.get("presentPaths")) if isinstance(path, str) and not _blank(path)}
    staged = sorted(set(_listed(commit.get("stagedPaths")))) if isinstance(commit.get("stagedPaths"), list) else []
    rejected = sorted(set(_listed(commit.get("rejectedPaths")))) if isinstance(commit.get("rejectedPaths"), list) else []
    missing = [path for path in planned
               if path not in set(staged) and path not in optional and path not in present]
    # A pushback (H16) is the implementer's case against a directive, not a delivery: the Lead decides
    # it at integration and the fork is never merged as-is.
    pushback = str(audit.get("state") or "") == "pushback"
    # A claim that the shared branch already satisfies the item is not a delivery either: the peer
    # reviewer decides it at integration (run 90e5e3aa).
    satisfied = str(audit.get("state") or "") == "satisfied"
    passed = not missing and not rejected and not pushback and not satisfied

    if pushback:
        state = "pushback"
    elif satisfied:
        state = "satisfied"
    elif passed:
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
        "state": state, "passed": passed, "plannedPaths": planned, "optionalPaths": sorted(optional),
        "presentPaths": sorted(present), "stagedPaths": staged, "missingPaths": missing,
        "rejectedPaths": rejected,
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
