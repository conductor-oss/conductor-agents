#!/usr/bin/env python3
"""Exercise safety-critical workflow paths in a live Conductor decision engine.

The endpoint executes the registered workflow definition while replacing worker
and remote side effects with declared task results.  SWITCH, DO_WHILE,
JSON_JQ_TRANSFORM, SET_VARIABLE, and output resolution are performed by the
server, so this catches definition/runtime mismatches without touching a repo or
GitHub.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path


BASE = os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080/api").rstrip("/")
TOKEN = os.environ.get("CONDUCTOR_AUTH_TOKEN", "")
_DEFINITIONS: dict[tuple[str, int], dict] = {}
WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "workers" / "workflows"


def mock(output: dict | None = None, status: str = "COMPLETED") -> list[dict]:
    return [{"status": status, "output": output or {}}]


def definition(name: str, version: int) -> dict:
    key = (name, version)
    if key in _DEFINITIONS:
        return deepcopy(_DEFINITIONS[key])
    local = WORKFLOW_DIR / f"{name}.json"
    if local.is_file():
        loaded = json.loads(local.read_text(encoding="utf-8"))
        if loaded.get("name") != name or loaded.get("version") != version:
            raise RuntimeError(f"local definition mismatch for {name} v{version}")
        _DEFINITIONS[key] = loaded
        return deepcopy(loaded)
    request = urllib.request.Request(f"{BASE}/metadata/workflow/{name}?version={version}")
    if TOKEN:
        request.add_header("X-Authorization", TOKEN)
    with urllib.request.urlopen(request) as response:  # noqa: S310
        loaded = json.loads(response.read())
    if loaded.get("name") != name or loaded.get("version") != version:
        raise RuntimeError(f"registered definition mismatch for {name} v{version}")
    _DEFINITIONS[key] = loaded
    return deepcopy(loaded)


def flatten_subworkflows(value) -> None:
    """Let the test endpoint mock child results while preserving parent control flow."""
    if isinstance(value, dict):
        if value.get("type") == "SUB_WORKFLOW":
            value["type"] = "SIMPLE"
            value.pop("subWorkflowParam", None)
        for child in value.values():
            flatten_subworkflows(child)
    elif isinstance(value, list):
        for child in value:
            flatten_subworkflows(child)


def execute_definition(name: str, version: int, inputs: dict,
                       mocks: dict[str, list[dict]], workflow: dict) -> dict:
    body = json.dumps({
        "name": name,
        "version": version,
        "input": inputs,
        "workflowDef": workflow,
        "taskRefToMockOutput": mocks,
    }).encode()
    request = urllib.request.Request(
        f"{BASE}/workflow/test",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if TOKEN:
        request.add_header("X-Authorization", TOKEN)
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise RuntimeError(f"{name} live test returned HTTP {exc.code}: {detail}") from exc


def execute(name: str, version: int, inputs: dict, mocks: dict[str, list[dict]]) -> dict:
    workflow = definition(name, version)
    flatten_subworkflows(workflow)
    return execute_definition(name, version, inputs, mocks, workflow)


def refs(execution: dict) -> set[str]:
    return {
        str(task.get("referenceTaskName") or task.get("taskRefName") or "")
        for task in execution.get("tasks") or []
    }


def require_output(execution: dict, **expected) -> None:
    output = execution.get("output") or execution.get("outputData") or {}
    for key, value in expected.items():
        actual = output.get(key)
        if actual != value:
            tasks = [
                {
                    "ref": task.get("referenceTaskName") or task.get("taskRefName"),
                    "status": task.get("status"),
                    "reason": task.get("reasonForIncompletion"),
                    "output": task.get("outputData"),
                }
                for task in execution.get("tasks") or []
            ]
            raise AssertionError(
                f"{key}: expected {value!r}, got {actual!r}; "
                f"status={execution.get('status')!r} reason={execution.get('reasonForIncompletion')!r} "
                f"output={output!r} tasks={tasks!r}"
            )


def require_absent(execution: dict, *task_refs: str) -> None:
    reached = refs(execution)
    unexpected = sorted(set(task_refs) & reached)
    if unexpected:
        raise AssertionError(f"remote mutation task(s) unexpectedly reached: {unexpected}")


def require_status(execution: dict, expected: str) -> None:
    actual = execution.get("status")
    if actual != expected:
        raise AssertionError(
            f"status: expected {expected!r}, got {actual!r}; "
            f"reason={execution.get('reasonForIncompletion')!r}"
        )


def require_task_input(execution: dict, task_ref: str, **expected) -> None:
    matches = [
        task for task in execution.get("tasks") or []
        if (task.get("referenceTaskName") or task.get("taskRefName")) == task_ref
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one task {task_ref!r}, found {len(matches)}")
    actual_input = matches[0].get("inputData") or {}
    for key, value in expected.items():
        if actual_input.get(key) != value:
            raise AssertionError(
                f"{task_ref}.{key}: expected {value!r}, got {actual_input.get(key)!r}; "
                f"input={actual_input!r}"
            )


def review_value(summary: str = "LGTM") -> dict:
    return {"summary": summary, "verdict": "approve", "comments": []}


def review_with_finding() -> dict:
    return {
        "summary": "Changes requested.",
        "verdict": "request_changes",
        "comments": [{
            "path": "src/retry.py", "line": 42, "severity": "blocking",
            "body": "Add the missing bound before incrementing the retry counter.",
        }],
    }


def review_mocks() -> dict[str, list[dict]]:
    return {
        "model_policy": mock({}),
        "fb": mock({"number": 12, "feedback": "Prior discussion."}),
        "clone": mock({"repoPath": "/tmp/live-contract-review"}),
        "co": mock({"head": "b" * 40}),
        "diff": mock({"diff": "diff --git a/a b/a", "changedFiles": ["a"]}),
        "review": mock({"structured": review_value(), "tokenUsed": 10, "costUsd": 0.01}),
    }


def scenario_review_auto_publish() -> None:
    mocks = review_mocks()
    mocks["submit"] = mock({
        "event": "APPROVE", "inlineCount": 0, "inline": True,
        "url": "https://example.invalid/review/auto",
    })
    execution = execute(
        "pr_review", 1, {"repo": "acme/app", "prNumber": 12, "approve": False}, mocks,
    )
    require_output(execution, approvalState="approved", publicationState="published",
                   reviewUrl="https://example.invalid/review/auto")
    require_task_input(execution, "submit", event="APPROVE",
                       structured={"summary": "LGTM", "verdict": "approve", "comments": []})


def scenario_review_auto_request_changes() -> None:
    mocks = review_mocks()
    mocks["review"] = mock({
        "structured": review_with_finding(), "tokenUsed": 10, "costUsd": 0.01,
    })
    mocks["submit"] = mock({
        "event": "REQUEST_CHANGES", "inlineCount": 1, "inline": True,
        "url": "https://example.invalid/review/auto-changes",
    })
    execution = execute(
        "pr_review", 1, {"repo": "acme/app", "prNumber": 12, "approve": False}, mocks,
    )
    require_output(execution, approvalState="changes_requested", publicationState="published",
                   event="REQUEST_CHANGES", reviewUrl="https://example.invalid/review/auto-changes")
    require_task_input(execution, "submit", event="REQUEST_CHANGES",
                       structured=review_with_finding())


def scenario_review_approve() -> None:
    mocks = review_mocks()
    mocks.update({
        "review_gate": mock({
            "approved": True, "action": "approve", "review": review_with_finding(),
        }),
        "submit": mock({
            "event": "APPROVE", "inlineCount": 0, "inline": True,
            "url": "https://example.invalid/review/approved",
        }),
    })
    execution = execute(
        "pr_review", 1, {"repo": "acme/app", "prNumber": 12, "approve": True}, mocks,
    )
    require_output(execution, approvalState="approved", publicationState="published",
                   reviewUrl="https://example.invalid/review/approved")
    expected = review_with_finding()
    expected.update({"summary": "LGTM", "verdict": "approve"})
    require_task_input(execution, "submit", event="APPROVE", structured=expected)


def scenario_review_stop() -> None:
    mocks = review_mocks()
    mocks["review_gate"] = mock({"approved": False, "action": "stop", "suppressed": True})
    execution = execute(
        "pr_review", 1, {"repo": "acme/app", "prNumber": 12, "approve": True}, mocks,
    )
    require_output(execution, approvalState="suppressed", publicationState="suppressed",
                   reviewUrl="")
    require_absent(execution, "submit")


def scenario_review_invalid_approve() -> None:
    mocks = review_mocks()
    mocks["review_gate"] = mock({"approved": False, "action": "approve"})
    execution = execute(
        "pr_review", 1, {"repo": "acme/app", "prNumber": 12, "approve": True}, mocks,
    )
    require_output(execution, approvalState="blocked", publicationState="blocked")
    require_absent(execution, "submit")


def scenario_review_empty_revision() -> None:
    mocks = review_mocks()
    mocks["review_gate"] = mock({"approved": False, "action": "revise", "feedback": "  "})
    execution = execute(
        "pr_review", 1, {"repo": "acme/app", "prNumber": 12, "approve": True}, mocks,
    )
    require_output(execution, approvalState="blocked", publicationState="blocked")
    require_absent(execution, "submit")


def scenario_review_request_changes() -> None:
    mocks = review_mocks()
    mocks.update({
        "review_gate": mock({
            "approved": False, "action": "revise",
            "feedback": "Add a regression test for the retry path.",
        }),
        "submit": mock({
            "event": "REQUEST_CHANGES", "inlineCount": 0, "inline": True,
            "url": "https://example.invalid/review/changes-requested",
        }),
    })
    execution = execute(
        "pr_review", 1, {"repo": "acme/app", "prNumber": 12, "approve": True}, mocks,
    )
    require_output(execution, approvalState="changes_requested", publicationState="published",
                   event="REQUEST_CHANGES",
                   reviewUrl="https://example.invalid/review/changes-requested",
                   tokenUsed=10, costUsd=0.01)
    require_task_input(
        execution, "submit", event="REQUEST_CHANGES",
        structured={"summary": "Add a regression test for the retry path.",
                    "verdict": "request_changes", "comments": []},
    )


def issue_mocks(state: str = "passed") -> dict[str, list[dict]]:
    return {
        "model_policy": mock({}),
        "issue": mock({"number": 17, "title": "Fix the edge", "body": "Handle the edge."}),
        "clone": mock({"repoPath": "/tmp/live-contract-issue"}),
        "cp": mock({
            "proposalText": "Implemented the focused fix.",
            "subtasks": [{"id": "fix-edge", "description": "Fix the edge"}],
            "verificationFindings": [],
            "verified": state == "passed",
            "verificationCommit": "a" * 40,
            "verificationState": state,
            "sourceHandoff": {
                "repoPath": "/tmp/live-contract-issue",
                "branch": "harness/issue-17",
            },
            "merged": ["fix-edge"],
            "conflicts": [],
            "totalTokens": 10,
            "totalCostUsd": 0.01,
        }),
    }


def revised_issue_output(state: str = "passed") -> dict:
    return {
        "proposalText": "Revised the candidate after validating feedback.",
        "subtasks": [{"id": "revise-edge", "description": "Revise the edge"}],
        "verificationFindings": [] if state == "passed" else ["targeted check failed"],
        "verified": state == "passed",
        "verificationCommit": "f" * 40,
        "verificationState": state,
        "verification": {"targeted-check": {"passed": state == "passed"}},
        "sourceHandoff": {
            "repoPath": "/tmp/live-contract-issue",
            "branch": "harness/issue-17",
        },
        "merged": ["revise-edge"],
        "conflicts": [],
        "totalTokens": 12,
        "totalCostUsd": 0.02,
    }


def address_mocks(state: str = "passed") -> dict[str, list[dict]]:
    return {
        "model_policy": mock({}),
        "fb": mock({
            "hasFeedback": True,
            "feedback": "Please cover the edge.",
            "headRepoUrl": "https://example.invalid/acme/fork.git",
            "headRepo": "acme/fork",
            "head": "feature",
            "headSha": "b" * 40,
            "number": 12,
            "commentCount": 1,
            "linkCount": 0,
            "linkWarnings": [],
            "linkedContextChars": 0,
        }),
        "clone": mock({"repoPath": "/tmp/live-contract-address"}),
        "co": mock({"head": "b" * 40}),
        "cp": mock({
            "agentCompleted": True,
            "proposalText": "Covered the requested edge.",
            "subtasks": [{"id": "cover-edge", "description": "Cover the edge"}],
            "verificationFindings": [],
            "verified": state == "passed",
            "verificationCommit": "c" * 40,
            "verificationState": state,
            "verification": {"commands": ["targeted-check"]},
            "sourceHandoff": {"repoPath": "/tmp/live-contract-address"},
            "totalTokens": 10,
        }),
        "verify_discover": mock({"candidates": [{"argv": ["targeted-check"]}]}),
        "verify": mock({
            "candidateCommit": "c" * 40,
            "verificationState": state,
            "commands": {"targeted-check": {"passed": state == "passed"}},
        }),
    }


def scenario_issue_unverified() -> None:
    execution = execute(
        "issue_to_pr", 1,
        {"repo": "acme/app", "issueNumber": 17, "approvePr": False},
        issue_mocks("failed"),
    )
    require_output(execution, approvalState="verification_blocked",
                   publicationState="verification_blocked", pushed=False)
    require_absent(execution, "push", "pr")


def scenario_issue_stop() -> None:
    mocks = issue_mocks()
    mocks["pr_gate__1"] = mock({"approved": False, "action": "stop", "suppressed": True})
    execution = execute(
        "issue_to_pr", 1,
        {"repo": "acme/app", "issueNumber": 17, "approvePr": True}, mocks,
    )
    require_output(execution, approvalState="suppressed", publicationState="suppressed", pushed=False)
    require_absent(execution, "push", "pr")


def scenario_issue_approve() -> None:
    mocks = issue_mocks()
    mocks.update({
        "pr_gate__1": mock({"approved": True, "action": "approve",
                            "title": "Focused fix", "body": "Closes #17"}),
        "push": mock({"pushed": True, "head": "a" * 40}),
        "pr": mock({"number": 99, "url": "https://example.invalid/pr/99",
                    "publicationState": "published"}),
    })
    execution = execute(
        "issue_to_pr", 1,
        {"repo": "acme/app", "issueNumber": 17, "approvePr": True}, mocks,
    )
    require_output(execution, approvalState="approved", publicationState="published",
                   pushed=True, prNumber=99)


def scenario_issue_auto_approve() -> None:
    mocks = issue_mocks()
    mocks.update({
        "push": mock({"pushed": True, "head": "a" * 40}),
        "pr": mock({"number": 98, "url": "https://example.invalid/pr/98",
                    "publicationState": "published"}),
    })
    execution = execute(
        "issue_to_pr", 1,
        {"repo": "acme/app", "issueNumber": 17, "approvePr": False}, mocks,
    )
    require_output(execution, approvalState="approved", publicationState="published",
                   pushed=True, prNumber=98)
    require_absent(execution, "pr_gate")


def scenario_issue_invalid_approve() -> None:
    mocks = issue_mocks()
    mocks["pr_gate__1"] = mock({"approved": False, "action": "approve"})
    execution = execute(
        "issue_to_pr", 1,
        {"repo": "acme/app", "issueNumber": 17, "approvePr": True}, mocks,
    )
    require_output(execution, approvalState="blocked", publicationState="blocked", pushed=False)
    require_absent(execution, "push", "pr")


def scenario_issue_pr_permission_blocked_retains_pushed_branch() -> None:
    mocks = issue_mocks()
    mocks.update({
        "push": mock({"pushed": True, "head": "a" * 40}),
        "pr": mock({"created": False, "number": 0, "url": "",
                    "publicationState": "permission_blocked",
                    "branch": "harness/issue-17"}),
    })
    execution = execute(
        "issue_to_pr", 1,
        {"repo": "acme/app", "issueNumber": 17, "approvePr": False}, mocks,
    )
    require_status(execution, "COMPLETED")
    require_output(execution, publicationState="permission_blocked", pushed=True,
                   changeBranch="harness/issue-17", prNumber=0)


def scenario_issue_empty_revision() -> None:
    mocks = issue_mocks()
    mocks["pr_gate__1"] = mock({"approved": False, "action": "revise", "feedback": "   "})
    execution = execute(
        "issue_to_pr", 1,
        {"repo": "acme/app", "issueNumber": 17, "approvePr": True}, mocks,
    )
    require_output(execution, approvalState="blocked", publicationState="blocked", pushed=False)
    require_absent(execution, "revise_candidate", "push", "pr")


def scenario_issue_revise_failed() -> None:
    mocks = issue_mocks()
    mocks.update({
        "pr_gate__1": mock({"approved": False, "action": "revise",
                            "feedback": "Add the missing edge assertion."}),
        "revise_candidate__1": mock(revised_issue_output("failed")),
    })
    execution = execute(
        "issue_to_pr", 1,
        {"repo": "acme/app", "issueNumber": 17, "approvePr": True}, mocks,
    )
    require_output(execution, approvalState="verification_blocked",
                   publicationState="verification_blocked", pushed=False,
                   verificationCommit="f" * 40)
    require_absent(execution, "push", "pr")


def scenario_issue_revise_then_approve() -> None:
    mocks = issue_mocks()
    mocks.update({
        "pr_gate__1": mock({"approved": False, "action": "revise",
                            "feedback": "Add the missing edge assertion."}),
        "revise_candidate__1": mock(revised_issue_output("passed")),
        "pr_gate__2": mock({"approved": True, "action": "approve",
                            "title": "Focused revised fix", "body": "Closes #17"}),
        "push": mock({"pushed": True, "head": "f" * 40}),
        "pr": mock({"number": 100, "url": "https://example.invalid/pr/100",
                    "publicationState": "published"}),
    })
    execution = execute(
        "issue_to_pr", 1,
        {"repo": "acme/app", "issueNumber": 17, "approvePr": True}, mocks,
    )
    require_output(execution, approvalState="approved", publicationState="published",
                   pushed=True, prNumber=100, verificationCommit="f" * 40)


def scenario_issue_revision_exhausted() -> None:
    mocks = issue_mocks()
    mocks.update({
        "pr_gate__1": mock({"approved": False, "action": "revise", "feedback": "One change."}),
        "revise_candidate__1": mock(revised_issue_output("passed")),
    })
    execution = execute(
        "issue_to_pr", 1,
        {"repo": "acme/app", "issueNumber": 17, "approvePr": True,
         "maxApprovalRevisions": 0}, mocks,
    )
    require_output(execution, approvalState="revision_exhausted",
                   publicationState="revision_exhausted", pushed=False)
    require_absent(execution, "push", "pr")


def scenario_address_no_feedback() -> None:
    execution = execute(
        "address_pr", 1,
        {"repo": "acme/app", "prNumber": 12},
        {"model_policy": mock({}), "fb": mock({"hasFeedback": False, "number": 12})},
    )
    require_output(execution, approvalState="no_feedback", publicationState="not_needed", pushed=False)
    require_absent(execution, "publish")


def scenario_address_unverified() -> None:
    mocks = address_mocks("failed")
    mocks["repair"] = mock({
        "candidateCommit": "d" * 40,
        "verificationState": "failed",
        "verification": {"targeted-check": {"passed": False}},
    })
    execution = execute(
        "address_pr", 1,
        {"repo": "acme/app", "prNumber": 12}, mocks,
    )
    require_output(execution, approvalState="verification_blocked",
                   publicationState="verification_blocked", pushed=False)
    require_absent(execution, "address_gate", "publish")


def scenario_address_repair_then_approve() -> None:
    mocks = address_mocks("failed")
    mocks.update({
        "repair": mock({
            "candidateCommit": "d" * 40,
            "verificationState": "passed",
            "verification": {"targeted-check": {"passed": True}},
        }),
        "address_gate__1": mock({"approved": True, "action": "approve",
                                 "body": "Addressed after repair."}),
        "publish": mock({
            "publicationState": "published",
            "pushed": True,
            "replyUrl": "https://example.invalid/comment/repaired",
            "ciState": "passed",
        }),
    })
    execution = execute(
        "address_pr", 1,
        {"repo": "acme/app", "prNumber": 12}, mocks,
    )
    require_output(execution, approvalState="approved", publicationState="published",
                   pushed=True, verificationCommit="d" * 40)


def scenario_address_direct_engine() -> None:
    mocks = address_mocks()
    mocks.update({
        "code": mock({"agentCompleted": True, "result": "Made the direct fix."}),
        "cmt": mock({"commit": "7" * 40}),
        "verify": mock({
            "candidateCommit": "7" * 40,
            "verificationState": "passed",
            "commands": {"targeted-check": {"passed": True}},
        }),
        "address_gate__1": mock({"approved": True, "action": "approve",
                                 "body": "Addressed directly."}),
        "publish": mock({
            "publicationState": "published", "pushed": True,
            "replyUrl": "https://example.invalid/comment/direct", "ciState": "passed",
        }),
    })
    execution = execute(
        "address_pr", 1,
        {"repo": "acme/app", "prNumber": 12, "engine": "coding_agent"}, mocks,
    )
    require_output(execution, approvalState="approved", publicationState="published",
                   pushed=True, verificationCommit="7" * 40)


def scenario_address_stop() -> None:
    mocks = address_mocks()
    mocks["address_gate__1"] = mock({"approved": False, "action": "stop", "suppressed": True})
    execution = execute(
        "address_pr", 1,
        {"repo": "acme/app", "prNumber": 12}, mocks,
    )
    require_output(execution, approvalState="suppressed", publicationState="suppressed", pushed=False)
    require_absent(execution, "publish")


def scenario_address_invalid_approve() -> None:
    mocks = address_mocks()
    mocks["address_gate__1"] = mock({"approved": False, "action": "approve"})
    execution = execute(
        "address_pr", 1,
        {"repo": "acme/app", "prNumber": 12}, mocks,
    )
    require_output(execution, approvalState="blocked", publicationState="blocked", pushed=False)
    require_absent(execution, "publish")


def scenario_address_empty_revision() -> None:
    mocks = address_mocks()
    mocks["address_gate__1"] = mock({"approved": False, "action": "revise", "feedback": "\t"})
    execution = execute(
        "address_pr", 1,
        {"repo": "acme/app", "prNumber": 12}, mocks,
    )
    require_output(execution, approvalState="blocked", publicationState="blocked", pushed=False)
    require_absent(execution, "revise_address_candidate", "publish")


def revised_address_output(state: str = "passed") -> dict:
    return {
        "proposalText": "Revised the address candidate.",
        "subtasks": [{"id": "revise-address", "description": "Revise address candidate"}],
        "verificationFindings": [] if state == "passed" else ["targeted check failed"],
        "verified": state == "passed",
        "verificationCommit": "9" * 40,
        "verificationState": state,
        "verification": {"targeted-check": {"passed": state == "passed"}},
        "totalTokens": 12,
    }


def scenario_address_revise_failed() -> None:
    mocks = address_mocks()
    mocks.update({
        "address_gate__1": mock({"approved": False, "action": "revise",
                                 "feedback": "Add the missing edge assertion."}),
        "revise_address_candidate__1": mock(revised_address_output("failed")),
    })
    execution = execute(
        "address_pr", 1,
        {"repo": "acme/app", "prNumber": 12}, mocks,
    )
    require_output(execution, approvalState="verification_blocked",
                   publicationState="verification_blocked", pushed=False,
                   verificationCommit="9" * 40)
    require_absent(execution, "publish")


def scenario_address_revise_then_approve() -> None:
    mocks = address_mocks()
    mocks.update({
        "address_gate__1": mock({"approved": False, "action": "revise",
                                 "feedback": "Add the missing edge assertion."}),
        "revise_address_candidate__1": mock(revised_address_output("passed")),
        "address_gate__2": mock({"approved": True, "action": "approve",
                                 "body": "Addressed after revision."}),
        "publish": mock({
            "publicationState": "published",
            "pushed": True,
            "replyUrl": "https://example.invalid/comment/2",
            "ciState": "passed",
        }),
    })
    execution = execute(
        "address_pr", 1,
        {"repo": "acme/app", "prNumber": 12}, mocks,
    )
    require_output(execution, approvalState="approved", publicationState="published",
                   pushed=True, verificationCommit="9" * 40)


def scenario_address_revision_exhausted() -> None:
    mocks = address_mocks()
    mocks.update({
        "address_gate__1": mock({"approved": False, "action": "revise",
                                 "feedback": "One more change."}),
        "revise_address_candidate__1": mock(revised_address_output("passed")),
    })
    execution = execute(
        "address_pr", 1,
        {"repo": "acme/app", "prNumber": 12, "maxApprovalRevisions": 0}, mocks,
    )
    require_output(execution, approvalState="revision_exhausted",
                   publicationState="revision_exhausted", pushed=False)
    require_absent(execution, "publish")


def scenario_address_publication_blocked() -> None:
    mocks = address_mocks()
    mocks.update({
        "address_gate__1": mock({"approved": True, "action": "approve",
                                 "body": "Ready to publish."}),
        "publish": mock({
            "publicationState": "ci_blocked", "pushed": True,
            "replyUrl": "", "ciState": "failed",
        }),
    })
    execution = execute(
        "address_pr", 1,
        {"repo": "acme/app", "prNumber": 12}, mocks,
    )
    require_output(execution, approvalState="approved", publicationState="ci_blocked",
                   pushed=True, ciVerificationState="failed")


def publisher_mocks(ci_state: str) -> dict[str, list[dict]]:
    return {
        "model_policy": mock({}),
        "candidate_guard": mock({"matched": True}),
        "branch_guard": mock({"verificationState": "matched"}),
        "push": mock({"pushed": True, "head": "e" * 40}),
        "ci": mock({"verificationState": ci_state, "checks": [], "links": []}),
    }


def publisher_input() -> dict:
    return {
        "repoPath": "/tmp/live-contract-publish",
        "repo": "acme/app",
        "headRepo": "acme/fork",
        "prNumber": 12,
        "branch": "feature",
        "expectedHeadSha": "b" * 40,
        "candidateCommit": "e" * 40,
        "replyBody": "Verified and addressed.",
    }


def scenario_publish_branch_drift() -> None:
    execution = execute(
        "publish_verified_pr", 1, publisher_input(), {
            "model_policy": mock({}),
            "candidate_guard": mock({"matched": True}),
            "branch_guard": mock({"verificationState": "branch_drift"}),
        },
    )
    require_output(execution, publicationState="branch_drift", pushed=False)
    require_absent(execution, "push", "ci", "reply")


def scenario_publish_candidate_guard_failure() -> None:
    execution = execute(
        "publish_verified_pr", 1, publisher_input(), {
            "model_policy": mock({}),
            "candidate_guard": mock(
                {"matched": False}, status="FAILED_WITH_TERMINAL_ERROR"),
        },
    )
    require_status(execution, "FAILED")
    require_absent(execution, "branch_guard", "push", "ci", "reply")


def scenario_publish_failed_ci() -> None:
    execution = execute("publish_verified_pr", 1, publisher_input(), publisher_mocks("failed"))
    require_output(execution, publicationState="ci_blocked", pushed=True, ciState="failed")
    require_absent(execution, "reply", "reply_after_poll")


def scenario_publish_unknown_ci() -> None:
    execution = execute("publish_verified_pr", 1, publisher_input(), publisher_mocks("unknown"))
    require_output(execution, publicationState="ci_blocked", pushed=True, ciState="unknown")
    require_absent(execution, "reply", "reply_after_poll")


def scenario_publish_passed_ci() -> None:
    mocks = publisher_mocks("passed")
    mocks["reply"] = mock({"url": "https://example.invalid/comment/1"})
    execution = execute("publish_verified_pr", 1, publisher_input(), mocks)
    require_output(execution, publicationState="published", pushed=True, ciState="passed",
                   replyUrl="https://example.invalid/comment/1")


def scenario_publish_permission_blocked_retains_branch() -> None:
    mocks = publisher_mocks("permission_blocked")
    mocks["push"] = mock({
        "pushed": False,
        "head": "e" * 40,
        "branch": "feature",
        "publicationState": "permission_blocked",
        "reason": "GitHub credential lacks permission to update workflow files",
    })
    execution = execute("publish_verified_pr", 1, publisher_input(), mocks)
    require_status(execution, "COMPLETED")
    require_task_input(execution, "ci", pushed=False, publicationState="permission_blocked")
    require_output(execution, branch="feature", publicationState="permission_blocked", pushed=False)
    require_absent(execution, "reply", "reply_after_poll")


def scenario_publish_empty_then_passed_ci() -> None:
    mocks = publisher_mocks("empty")
    mocks.update({
        "ci_wait__1": mock({}),
        "ci_poll_check__1": mock({
            "verificationState": "passed",
            "checks": [{"name": "targeted-ci", "state": "passed"}],
            "links": [],
        }),
        "reply_after_poll": mock({"url": "https://example.invalid/comment/3"}),
    })
    execution = execute("publish_verified_pr", 1, publisher_input(), mocks)
    require_output(execution, publicationState="published", pushed=True, ciState="passed",
                   replyUrl="https://example.invalid/comment/3")


def scenario_publish_pending_exhausted() -> None:
    mocks = publisher_mocks("pending")
    for iteration in range(1, 7):
        mocks[f"ci_wait__{iteration}"] = mock({})
        mocks[f"ci_poll_check__{iteration}"] = mock({
            "verificationState": "pending", "checks": [], "links": [],
        })
    execution = execute("publish_verified_pr", 1, publisher_input(), mocks)
    require_output(execution, publicationState="ci_blocked", pushed=True, ciState="pending")
    require_absent(execution, "reply", "reply_after_poll")


def code_parallel_candidate_contract() -> dict:
    registered = definition("code_parallel", 1)
    tasks = {task["taskReferenceName"]: deepcopy(task) for task in registered["tasks"]}
    verify_loop = tasks["verify_loop"]
    verify_action = next(
        task for task in verify_loop["loopOver"]
        if task["taskReferenceName"] == "verify_action"
    )
    candidate_advance = deepcopy(next(
        task for task in verify_action["decisionCases"]["true"]
        if task["taskReferenceName"] == "verification_candidate_advance"
    ))
    verify_loop["loopOver"] = [
        {
            "name": "candidate_round",
            "taskReferenceName": "candidate_round",
            "type": "SIMPLE",
            "inputParameters": {},
        },
        {
            "name": "candidate_round_state",
            "taskReferenceName": "candidate_round_state",
            "type": "SET_VARIABLE",
            "inputParameters": {"verifyPassed": "${candidate_round.output.passed}"},
        },
        {
            "name": "candidate_round_action",
            "taskReferenceName": "candidate_round_action",
            "type": "SWITCH",
            "evaluatorType": "value-param",
            "expression": "notPassed",
            "inputParameters": {"notPassed": "${candidate_round.output.notPassed}"},
            "decisionCases": {
                "true": [
                    {
                        "name": "commit",
                        "taskReferenceName": "verify_fixup_commit",
                        "type": "SIMPLE",
                        "inputParameters": {},
                    },
                    candidate_advance,
                ],
            },
            "defaultCase": [],
        },
    ]
    verify = tasks["verify"]
    verify["type"] = "SIMPLE"
    verify.pop("subWorkflowParam", None)
    verify["inputParameters"]["allowedWriteRoots"] = []
    return {
        "name": "code_parallel_candidate_contract",
        "version": 1,
        "schemaVersion": 2,
        "variables": deepcopy(registered["variables"]),
        "inputParameters": ["verifyMaxIterations"],
        "inputTemplate": {"verifyMaxIterations": 3},
        "tasks": [
            {
                "name": "workspace",
                "taskReferenceName": "workspace",
                "type": "SIMPLE",
                "inputParameters": {},
            },
            {
                "name": "merge",
                "taskReferenceName": "merge",
                "type": "SIMPLE",
                "inputParameters": {},
            },
            tasks["verification_candidate_init"],
            verify_loop,
            verify,
            tasks["verification_outcome"],
            tasks["source_handoff"],
            tasks["handoff_summary"],
        ],
        "outputParameters": {
            "verificationCommit": "${verify.output.candidateCommit}",
            "handoffCommit": "${source_handoff.output.head}",
            "verificationState": "${verification_outcome.output.result.state}",
            "presented": "${handoff_summary.output.result.presented}",
        },
    }


def code_parallel_mocks(rounds: list[tuple[bool, str | None]]) -> dict[str, list[dict]]:
    merge_commit = "a" * 40
    mocks = {
        "workspace": mock({
            "worktreePath": "/tmp/live-contract-code-parallel",
            "branch": "harness/live-contract",
        }),
        "merge": mock({
            "mergeState": "merged",
            "merge": {"commit": merge_commit},
        }),
    }
    for iteration, (passed, fixup_commit) in enumerate(rounds, start=1):
        suffix = f"__{iteration}"
        mocks[f"candidate_round{suffix}"] = mock({
            "passed": passed, "notPassed": not passed,
        })
        if not passed:
            mocks[f"verify_fixup_commit{suffix}"] = mock({"commit": fixup_commit})
    return mocks


def scenario_code_parallel_candidate_without_fixup() -> None:
    candidate = "a" * 40
    mocks = code_parallel_mocks([(True, None)])
    mocks.update({
        "verify": mock({
            "candidateCommit": candidate, "verificationState": "passed",
            "executionOutcome": "passed", "attempts": 1, "verification": {},
        }),
        "source_handoff": mock({
            "repoPath": "/tmp/live-contract-code-parallel",
            "branch": "harness/live-contract", "head": candidate, "matched": True,
        }),
    })
    workflow = code_parallel_candidate_contract()
    execution = execute_definition(
        workflow["name"], 1, {"verifyMaxIterations": 1}, mocks, workflow,
    )
    require_status(execution, "COMPLETED")
    require_task_input(execution, "verify", candidateCommit=candidate)
    require_task_input(execution, "source_handoff", expectedHead=candidate)
    require_output(execution, verificationCommit=candidate)


def scenario_code_parallel_candidate_after_repeated_fixups() -> None:
    first_fixup = "b" * 40
    latest_fixup = "c" * 40
    mocks = code_parallel_mocks([
        (False, first_fixup),
        (False, latest_fixup),
        (True, None),
    ])
    mocks.update({
        "verify": mock({
            "candidateCommit": latest_fixup, "verificationState": "passed",
            "executionOutcome": "passed", "attempts": 1, "verification": {},
        }),
        "source_handoff": mock({
            "repoPath": "/tmp/live-contract-code-parallel",
            "branch": "harness/live-contract", "head": latest_fixup, "matched": True,
        }),
    })
    workflow = code_parallel_candidate_contract()
    execution = execute_definition(
        workflow["name"], 1, {"verifyMaxIterations": 3}, mocks, workflow,
    )
    require_status(execution, "COMPLETED")
    require_task_input(execution, "verify", candidateCommit=latest_fixup)
    require_task_input(execution, "source_handoff", expectedHead=latest_fixup)
    require_output(execution, verificationCommit=latest_fixup)


def scenario_code_parallel_partial_merge_cannot_present() -> None:
    candidate = "a" * 40
    mocks = code_parallel_mocks([(True, None)])
    mocks["merge"] = mock({"mergeState": "conflicted", "merge": {"commit": candidate}})
    mocks.update({
        "verify": mock({
            "candidateCommit": candidate, "verificationState": "passed",
            "executionOutcome": "passed", "attempts": 1, "verification": {},
        }),
        "source_handoff": mock({
            "repoPath": "/tmp/live-contract-code-parallel",
            "branch": "harness/live-contract", "head": candidate, "matched": True,
        }),
    })
    workflow = code_parallel_candidate_contract()
    execution = execute_definition(
        workflow["name"], 1, {"verifyMaxIterations": 1}, mocks, workflow,
    )
    require_status(execution, "COMPLETED")
    require_output(execution, verificationState="blocked", presented=False)


def scenario_verification_preserves_required_commands() -> None:
    required = {
        "argv": ["pytest", "tests/e2e/test_workflow.py"],
        "source": "declared-plan-check",
        "scope": "focused",
        "affectedUnit": "workflow",
    }
    discovered = {
        "argv": ["pytest", "tests/unit/test_worker.py"],
        "source": "python-changed-test",
        "scope": "focused",
        "affectedUnit": "unit",
    }
    execution = execute("test_cycle", 1, {
        "repoPath": "/tmp/live-contract-verification",
        "candidateCommit": "d" * 40,
        "maxAttempts": 1,
        "priorVerification": {
            "verificationState": "failed",
            "executionOutcome": "code_failed",
            "commands": [required],
        },
    }, {
        "model_policy": mock({}),
        "discover__1": mock({
            "candidates": [discovered], "changedPaths": ["src/worker.py"],
            "executionOutcome": "discovered",
        }),
        "verify__1": mock({
            "candidateCommit": "d" * 40, "verificationState": "passed",
            "executionOutcome": "passed", "commands": [],
        }),
    })
    require_status(execution, "COMPLETED")
    require_task_input(execution, "verify__1", commands=[required, discovered])
    require_output(execution, verificationState="passed")


SCENARIOS = (
    scenario_review_auto_publish,
    scenario_review_auto_request_changes,
    scenario_review_approve,
    scenario_review_stop,
    scenario_review_invalid_approve,
    scenario_review_empty_revision,
    scenario_review_request_changes,
    scenario_issue_unverified,
    scenario_issue_stop,
    scenario_issue_approve,
    scenario_issue_auto_approve,
    scenario_issue_pr_permission_blocked_retains_pushed_branch,
    scenario_issue_invalid_approve,
    scenario_issue_empty_revision,
    scenario_issue_revise_failed,
    scenario_issue_revise_then_approve,
    scenario_issue_revision_exhausted,
    scenario_address_no_feedback,
    scenario_address_unverified,
    scenario_address_repair_then_approve,
    scenario_address_direct_engine,
    scenario_address_stop,
    scenario_address_invalid_approve,
    scenario_address_empty_revision,
    scenario_address_revise_failed,
    scenario_address_revise_then_approve,
    scenario_address_revision_exhausted,
    scenario_address_publication_blocked,
    scenario_publish_branch_drift,
    scenario_publish_candidate_guard_failure,
    scenario_publish_failed_ci,
    scenario_publish_unknown_ci,
    scenario_publish_passed_ci,
    scenario_publish_permission_blocked_retains_branch,
    scenario_publish_empty_then_passed_ci,
    scenario_publish_pending_exhausted,
    scenario_code_parallel_candidate_without_fixup,
    scenario_code_parallel_candidate_after_repeated_fixups,
    scenario_code_parallel_partial_merge_cannot_present,
    scenario_verification_preserves_required_commands,
)


def main() -> int:
    failed = 0
    selected = set(sys.argv[1:])
    scenarios = tuple(
        scenario for scenario in SCENARIOS
        if not selected or scenario.__name__.removeprefix("scenario_") in selected
    )
    for scenario in scenarios:
        name = scenario.__name__.removeprefix("scenario_")
        try:
            scenario()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}", file=sys.stderr)
    print(f"live paths: {len(scenarios) - failed}/{len(scenarios)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
