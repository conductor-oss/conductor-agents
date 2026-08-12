#!/usr/bin/env python3
"""Exercise fail-soft delivery and bounded repairs in Conductor's test engine."""

from __future__ import annotations

from validate_live_paths import execute, mock, refs, require_output, require_status, require_task_input


PLANNED = ["assets/result.png"]
SHA = "a" * 40


def _subtask_mocks(initial_state: str, repair_states: list[str], *, staged=None,
                   rejected=None, agent_completed: bool = True) -> dict:
    mocks = {
        "model_policy": mock({}),
        "wt": mock({"worktreePath": "/tmp/fail-soft-subtask", "branch": "group-asset"}),
        "code": mock({"status": "success", "agentCompleted": agent_completed,
                      "structured": {"phase": "initial"}, "denials": []}),
        "initial_delivery_audit": mock({
            "state": initial_state,
            "auditAvailable": initial_state != "audit_unavailable",
            "missingPaths": [] if initial_state == "passed" else PLANNED,
            "rejectedPaths": [], "actualPaths": PLANNED if initial_state == "passed" else [],
        }),
        "cmt": mock({"commit": SHA, "stagedPaths": staged if staged is not None else PLANNED,
                     "rejectedPaths": rejected or [], "noOp": not bool(staged if staged is not None else PLANNED)}),
    }
    for index, state in enumerate(repair_states, start=1):
        mocks[f"delivery_repair__{index}"] = mock({
            "status": "success", "agentCompleted": agent_completed,
            "structured": {"repairPass": index}, "denials": [],
        })
        mocks[f"repair_delivery_audit__{index}"] = mock({
            "state": state,
            "auditAvailable": state != "audit_unavailable",
            "missingPaths": [] if state == "passed" else PLANNED,
            "rejectedPaths": rejected or [],
            "actualPaths": PLANNED if state == "passed" else [],
        })
    return mocks


def _run_subtask(name: str, initial_state: str, repair_states: list[str],
                 expected_state: str, expected_attempts: int, **kwargs) -> None:
    execution = execute(
        "code_subtask", 1,
        {"repoPath": "/tmp/source", "name": name, "prompt": "Create the PNG",
         "allowedWriteRoots": PLANNED},
        _subtask_mocks(initial_state, repair_states, **kwargs),
    )
    if execution.get("status") != "COMPLETED":
        raise AssertionError({
            "status": execution.get("status"),
            "reason": execution.get("reasonForIncompletion"),
            "tasks": [{
                "ref": task.get("referenceTaskName"),
                "status": task.get("status"),
                "reason": task.get("reasonForIncompletion"),
                "output": task.get("outputData"),
            } for task in execution.get("tasks") or []],
        })
    require_status(execution, "COMPLETED")
    require_output(execution, deliveryOutcome=expected_state, repairAttempts=expected_attempts)
    reached = refs(execution)
    if expected_attempts == 0 and any(ref.startswith("delivery_repair__") for ref in reached):
        raise AssertionError("initial success unexpectedly entered the repair loop")


def validate_repairs() -> None:
    _run_subtask("initial", "passed", [], "passed", 0)
    _run_subtask("repair-one", "incomplete_delivery", ["passed"], "passed", 1)
    _run_subtask("repair-two", "incomplete_delivery",
                 ["incomplete_delivery", "passed"], "passed", 2)
    _run_subtask("repair-three", "incomplete_delivery",
                 ["incomplete_delivery", "incomplete_delivery", "passed"], "passed", 3)
    _run_subtask("persistent-missing", "incomplete_delivery",
                 ["incomplete_delivery"] * 3, "incomplete_delivery", 3, staged=[])
    _run_subtask("audit-failure", "audit_unavailable",
                 ["audit_unavailable"] * 3, "audit_unavailable", 3, staged=[])
    _run_subtask("agent-failure", "incomplete_delivery",
                 ["incomplete_delivery"] * 3, "implementation_unavailable", 3,
                 staged=[], agent_completed=False)
    _run_subtask("rejected-binary", "incomplete_delivery",
                 ["incomplete_delivery"] * 3, "incomplete_delivery", 3,
                 staged=[], rejected=["build/result.png"])


def _issue_mocks(child_output: dict | None, *, child_failed: bool = False) -> dict:
    child = (mock(status="FAILED") * 4) if child_failed else mock(child_output or {})
    return {
        "model_policy": mock({}),
        "issue": mock({"number": 17, "title": "Create result image", "body": "Add the PNG."}),
        "clone": mock({"repoPath": "/tmp/fail-soft-issue"}),
        "publication_workspace": mock({
            "worktreePath": "/tmp/fail-soft-issue/.worktrees/run",
            "branch": "harness/issue-17-run", "originalHead": SHA,
        }),
        "cp": child,
        "publication_commit": mock({"commit": SHA, "stagedPaths": [],
                                    "rejectedPaths": [], "noOp": True}),
        "publication_workspace_state": mock({"head": SHA, "branch": "harness/issue-17-run"}),
        "delivery_verification": mock({
            "candidateCommit": SHA,
            "verificationState": "passed",
            "attempts": 0,
            "repairReports": [],
            "verification": {"commands": []},
        }),
        "push": mock({"pushed": True, "head": SHA, "publicationState": "published"}),
        "pr": mock({"number": 42, "url": "https://example.invalid/pull/42",
                    "publicationState": "published"}),
    }


def _run_issue(label: str, outcome: str, attempts: int, *, child_failed: bool = False) -> None:
    child = {
        "deliveryOutcome": outcome,
        "verificationState": outcome,
        "verificationCommit": SHA,
        "proposalText": label,
        "deliveryRepairAttempts": attempts,
        "deliveryReports": [], "missingPaths": PLANNED if outcome != "passed" else [],
        "rejectedPaths": [], "commandFailures": [], "sandboxDenials": [],
        "subtasks": [], "merged": [], "conflicts": [],
    }
    execution = execute(
        "issue_to_pr", 1,
        {"repo": "acme/app", "issueNumber": 17, "approvePr": False},
        _issue_mocks(child, child_failed=child_failed),
    )
    if execution.get("status") != "COMPLETED":
        raise AssertionError({
            "status": execution.get("status"),
            "reason": execution.get("reasonForIncompletion"),
            "tasks": [{
                "ref": task.get("referenceTaskName"),
                "status": task.get("status"),
                "reason": task.get("reasonForIncompletion"),
                "output": task.get("outputData"),
                "optional": task.get("optional"),
            } for task in execution.get("tasks") or []],
        })
    require_status(execution, "COMPLETED")
    expected = "implementation_unavailable" if child_failed else outcome
    require_output(execution, deliveryOutcome=expected, prDraft=expected != "passed",
                   prNumber=42, pushed=True, verificationCommit=SHA)
    require_task_input(execution, "pr", draft=expected != "passed",
                       head="harness/issue-17-run")


def validate_publication() -> None:
    _run_issue("initial success", "passed", 0)
    _run_issue("repair one", "passed", 1)
    _run_issue("repair two", "passed", 2)
    _run_issue("repair three", "passed", 3)
    for outcome in ("incomplete_delivery", "merge_blocked", "audit_unavailable",
                    "implementation_unavailable"):
        _run_issue(outcome, outcome, 3)
    _run_issue("subworkflow failure", "implementation_unavailable", 0, child_failed=True)


def main() -> int:
    validate_repairs()
    validate_publication()
    print("fail-soft delivery: 17/17 live Conductor paths passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
