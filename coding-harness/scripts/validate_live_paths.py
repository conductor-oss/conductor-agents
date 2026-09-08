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


CAMPAIGN_REPO_URL = "https://github.com/acme/app"
CAMPAIGN_SOURCE = "/tmp/live-contract-campaign"
CAMPAIGN_WORKTREE = "/tmp/live-contract-campaign/.worktrees/run-live"
# workspace_prepare run-scopes the requested branch (git.allocate_outcome_branch),
# so the branch that exists is never the one campaign_defaults asked for.
CAMPAIGN_BRANCH = "feature-campaign/run-live"
CAMPAIGN_COMMIT = "9" * 40


def campaign_workspace_output(in_place: bool = False) -> dict:
    path = CAMPAIGN_SOURCE if in_place else CAMPAIGN_WORKTREE
    return {
        "sourceRepoPath": CAMPAIGN_SOURCE, "worktreePath": path,
        "branch": CAMPAIGN_BRANCH, "baseCommit": "a" * 40,
        "originalBranch": "main", "originalHead": "b" * 40, "sourceHead": "b" * 40,
        "baselineCommit": "c" * 40, "baselineCreated": True, "baselineIncludedPaths": [],
        "ignoredSourceChanges": 0, "ignoredSourcePaths": [], "materializedSourcePaths": [],
        "owned": not in_place, "resumed": False, "sourceCloned": not in_place,
        "inPlace": in_place,
    }


def campaign_phase_budget() -> dict:
    """Every campaign phase hands the next one its remaining turn/budget ceiling."""
    return {"finalMaxTurns": 500, "finalMaxBudgetUsd": 50}


def campaign_mocks(*, in_place: bool = False, design_approved: bool = True,
                   design_stopped: bool = False, plan_approved: bool = True,
                   plan_stopped: bool = False, implementation_outcome: str = "running",
                   implementation_remaining: list | None = None,
                   implementation_done: bool = True, final_outcome: str = "verified",
                   tests_passed: bool = True) -> dict[str, list[dict]]:
    """Happy-path mocks for feature_campaign; each scenario overrides what it drives.

    Keys are task reference names. The six SUB_WORKFLOW children are flattened to
    SIMPLE by execute(), so each is mocked at its own reference rather than being
    run -- these scenarios prove the campaign's own 16 gates, not its children's.
    """
    remaining = [] if implementation_remaining is None else implementation_remaining
    test_state = "passed" if tests_passed else "failed"
    return {
        "model_policy": mock({}),
        "campaign_issue": mock({"number": 17, "title": "Fix the edge",
                                "body": "Handle the edge.",
                                "url": "https://example.invalid/issues/17"}),
        "campaign_workspace": mock(campaign_workspace_output(in_place)),
        "validate_imported_plan": mock({"plan": {"tasks": [{"id": "t1"}]},
                                        "remainingTaskIds": ["t1"],
                                        "allowedWriteRoots": ["src"],
                                        "planLocation": ".conductor-code/openspec-plan.json"}),
        "campaign_design_docs": mock({
            "approved": design_approved, "stopped": design_stopped,
            "designLocation": "docs/design/widget.md", "sessionId": "design-1",
            "promptTemplate": {}, "feedback": "", **campaign_phase_budget()}),
        "campaign_design_commit": mock({"commit": "d" * 40, "committed": True}),
        "campaign_plan_approval": mock({
            "approved": plan_approved, "stopped": plan_stopped,
            "plan": {"tasks": [{"id": "t1"}]}, "planLocation": "plan.json",
            "remainingTaskIds": ["t1"], "allowedWriteRoots": ["src"],
            "sessionId": "plan-1", "promptTemplate": {}, "feedback": "",
            **campaign_phase_budget()}),
        "campaign_plan_edits_commit": mock({"commit": "e" * 40, "committed": True}),
        "campaign_inplace_checkpoint": mock({"commit": "1" * 40, "committed": True}),
        "campaign_implementation_waves": mock({
            "outcome": implementation_outcome, "completedTaskIds": ["t1"],
            "completedWaves": [1], "remainingTaskIds": remaining, "blockedTaskIds": [],
            "sessions": {}, "reviewSession": "", "reconcileSession": "",
            "lastReviewFindings": "", "feedback": "", **campaign_phase_budget()}),
        "implementation_done": mock({"done": implementation_done}),
        "campaign_final_verification": mock({
            "outcome": final_outcome, "feedback": "", "reviewFindings": "",
            "sessionId": "final-1", "promptTemplate": {}, "commit": CAMPAIGN_COMMIT,
            **campaign_phase_budget()}),
        "campaign_commit": mock({"commit": CAMPAIGN_COMMIT, "committed": True}),
        "campaign_tests": mock({"testsPassed": tests_passed, "testCycleState": test_state,
                                "testReport": {}, "candidateCommit": CAMPAIGN_COMMIT,
                                "agentAuthoredTest": False}),
        "campaign_publication_plan": mock({
            "draft": final_outcome != "verified" or not tests_passed,
            "outcome": final_outcome, "head": CAMPAIGN_COMMIT,
            "testCycleState": test_state, "testsPassed": tests_passed,
            "agentAuthoredTest": False, "agentAuthoredTestNote": ""}),
        "campaign_pr_approval": mock({
            "approvalState": "approved", "branch": CAMPAIGN_BRANCH,
            "workspacePath": CAMPAIGN_WORKTREE, "candidateCommit": CAMPAIGN_COMMIT,
            "title": "Add a widget", "body": "Adds the widget."}),
        "campaign_push": mock({"pushed": True, "head": CAMPAIGN_COMMIT}),
        "campaign_pr": mock({"number": 42, "url": "https://example.invalid/pr/42",
                             "publicationState": "published"}),
        "campaign_summary": mock({
            "branch": CAMPAIGN_BRANCH, "changedFiles": ["src/widget.py"],
            "candidateCommit": CAMPAIGN_COMMIT, "repoPath": CAMPAIGN_WORKTREE,
            "sourceRepoPath": CAMPAIGN_SOURCE, "sessions": {}, "totalCostUsd": 0.1,
            "totalTokens": 100, "verifiedBranch": CAMPAIGN_BRANCH,
            "uncommittedFiles": []}),
        "campaign_workspace_cleanup": mock({"removed": False, "retained": True,
                                            "reason": "keepWorktree"}),
    }


def campaign_input(**overrides) -> dict:
    inputs = {"repo": CAMPAIGN_REPO_URL, "instruction": "Add a widget",
              "issueNumber": 0}
    inputs.update(overrides)
    return inputs


def campaign(inputs: dict, mocks: dict[str, list[dict]]) -> dict:
    return execute("feature_campaign", 1, inputs, mocks)


# -- workspace source resolution -------------------------------------------
def scenario_campaign_clone_from_repo_url() -> None:
    """Regression test for the reported break.

    A campaign given only `repo` (no local checkout) must forward it to
    workspace_prepare as `repoUrl`. An obsolete registered definition that wired
    only `repoPath` dropped it, and every clone-from-GitHub campaign died with
    "workspace_prepare requires repoPath or repoUrl" -- see execution
    f69afa80-6c1b-48c3-a116-cffb688cfb4e.
    """
    execution = campaign(campaign_input(), campaign_mocks())
    require_status(execution, "COMPLETED")
    require_task_input(execution, "campaign_workspace",
                       repoUrl=CAMPAIGN_REPO_URL, repoPath="", workspacePath="")


def scenario_campaign_local_repo_path() -> None:
    execution = campaign(campaign_input(repo="", repoPath=CAMPAIGN_SOURCE),
                         campaign_mocks())
    require_status(execution, "COMPLETED")
    require_task_input(execution, "campaign_workspace",
                       repoPath=CAMPAIGN_SOURCE, repoUrl="", workspacePath="")


def scenario_campaign_inherited_workspace() -> None:
    """The openspec_development route hands the campaign an existing worktree."""
    execution = campaign(
        campaign_input(repo="", repoPath=CAMPAIGN_WORKTREE,
                       workspacePath=CAMPAIGN_WORKTREE),
        campaign_mocks())
    require_status(execution, "COMPLETED")
    require_task_input(execution, "campaign_workspace",
                       workspacePath=CAMPAIGN_WORKTREE, repoPath=CAMPAIGN_WORKTREE)


# -- instruction source ----------------------------------------------------
def scenario_campaign_issue_driven() -> None:
    execution = campaign(campaign_input(instruction="", issueNumber=17),
                         campaign_mocks())
    require_status(execution, "COMPLETED")
    require_output(execution, issueNumber=17, issueTitle="Fix the edge",
                   issueUrl="https://example.invalid/issues/17")
    if "campaign_issue_instruction" not in refs(execution):
        raise AssertionError("issue-driven campaign did not set the issue instruction")


def scenario_campaign_instruction_driven() -> None:
    execution = campaign(campaign_input(), campaign_mocks())
    require_status(execution, "COMPLETED")
    require_absent(execution, "campaign_issue", "campaign_issue_instruction")
    if "campaign_local_instruction" not in refs(execution):
        raise AssertionError("instruction-driven campaign did not set the local instruction")


def scenario_campaign_rejects_neither_instruction_nor_issue() -> None:
    """campaign_defaults is the only guard: neither key is schema-required."""
    execution = campaign(campaign_input(instruction="", issueNumber=0),
                         campaign_mocks())
    require_status(execution, "FAILED")
    require_absent(execution, "campaign_workspace")


def scenario_campaign_imported_plan_adopted() -> None:
    """An imported plan skips both authoring phases without tripping their limits."""
    execution = campaign(
        campaign_input(useImportedPlan=True,
                       importedPlan={"tasks": [{"id": "t1"}]}),
        campaign_mocks())
    require_status(execution, "COMPLETED")
    reached = refs(execution)
    for expected in ("validate_imported_plan", "adopt_imported_plan"):
        if expected not in reached:
            raise AssertionError(f"imported-plan campaign never reached {expected}")
    require_absent(execution, "campaign_design_docs", "campaign_plan_approval")


# -- design phase ----------------------------------------------------------
def scenario_campaign_design_stop() -> None:
    """A stopped design must not fall through into the plan phase.

    plan_needed_gate used to branch on planApproved alone -- the only phase gate
    that ignored `outcome` -- so a campaign already given up on still paid for a
    full dag_plan_approval sub-workflow.
    """
    execution = campaign(campaign_input(),
                         campaign_mocks(design_approved=False, design_stopped=True))
    require_status(execution, "COMPLETED")
    require_output(execution, outcome="incomplete")
    require_absent(execution, "campaign_plan_approval", "campaign_implementation_waves",
                   "campaign_final_verification")


def scenario_campaign_design_limit_reached() -> None:
    execution = campaign(campaign_input(designMaxRevisions=1),
                         campaign_mocks(design_approved=False))
    require_status(execution, "COMPLETED")
    require_output(execution, outcome="incomplete")
    if "design_limit_reached" not in refs(execution):
        raise AssertionError("unapproved design did not reach design_limit_reached")
    require_absent(execution, "campaign_plan_approval", "campaign_implementation_waves")


# -- plan phase ------------------------------------------------------------
def scenario_campaign_plan_stop() -> None:
    execution = campaign(campaign_input(),
                         campaign_mocks(plan_approved=False, plan_stopped=True))
    require_status(execution, "COMPLETED")
    require_output(execution, outcome="incomplete", completedDagTasks=[])
    require_absent(execution, "campaign_implementation_waves", "campaign_final_verification")


def scenario_campaign_plan_limit_reached() -> None:
    execution = campaign(campaign_input(planMaxRevisions=1),
                         campaign_mocks(plan_approved=False))
    require_status(execution, "COMPLETED")
    require_output(execution, outcome="incomplete")
    if "plan_limit_reached" not in refs(execution):
        raise AssertionError("unapproved plan did not reach plan_limit_reached")
    require_absent(execution, "campaign_implementation_waves", "campaign_final_verification")


# -- implementation and final phases ---------------------------------------
def scenario_campaign_implementation_incomplete() -> None:
    """Waves that end with DAG tasks left must stop the campaign, not verify it."""
    execution = campaign(
        campaign_input(),
        campaign_mocks(implementation_remaining=["t2"], implementation_done=False))
    require_status(execution, "COMPLETED")
    require_output(execution, outcome="incomplete")
    if "implementation_limit_reached" not in refs(execution):
        raise AssertionError("unfinished implementation did not reach the limit branch")
    require_absent(execution, "campaign_final_verification")


def scenario_campaign_final_verified() -> None:
    """The whole happy path: verified, tests green, non-draft PR opened."""
    execution = campaign(campaign_input(), campaign_mocks())
    require_status(execution, "COMPLETED")
    require_output(execution, outcome="verified", publicationState="published",
                   pushed=True, prNumber=42, testsPassed=True,
                   prApprovalState="approved")
    require_task_input(execution, "campaign_publication_plan", requestedDraft=False,
                       outcome="verified", testsPassed=True)
    # The branch handed to final_verification must be the one workspace_prepare
    # actually created, not campaign_defaults' pre-run-scoped request: it is shown
    # on final_verification's approval card, so a stale value asks an operator to
    # sign off against a branch that does not exist.
    require_task_input(execution, "campaign_final_verification",
                       branch=CAMPAIGN_BRANCH, repoPath=CAMPAIGN_WORKTREE)
    if "final_verified" not in refs(execution):
        raise AssertionError("verified campaign did not reach final_verified")


def scenario_campaign_final_limit_reached() -> None:
    """final_limit_gate's default catches any outcome that is neither terminal state."""
    execution = campaign(campaign_input(), campaign_mocks(final_outcome="running"))
    require_status(execution, "COMPLETED")
    require_output(execution, outcome="incomplete")
    if "final_limit_reached" not in refs(execution):
        raise AssertionError("unterminated final outcome did not reach final_limit_reached")


# -- test cycle ------------------------------------------------------------
def scenario_campaign_tests_failed() -> None:
    """Failed tests downgrade the campaign but still deliver -- as a draft PR."""
    execution = campaign(campaign_input(), campaign_mocks(tests_passed=False))
    require_status(execution, "COMPLETED")
    require_output(execution, outcome="incomplete", testsPassed=False,
                   publicationState="published")
    if "tests_not_passed" not in refs(execution):
        raise AssertionError("failing tests did not reach tests_not_passed")


def scenario_campaign_tests_subworkflow_failed() -> None:
    """campaign_tests is optional: a failed test_cycle must not fail the campaign.

    tests_outcome_gate then reads an absent testsPassed, takes its default branch
    and downgrades the campaign to incomplete -- a real test failure and a broken
    test_cycle are deliberately indistinguishable here, both fail soft.

    Four mocked attempts, not one: flatten_subworkflows() turns campaign_tests
    into a SIMPLE task, and `test_cycle` has no taskdef (it is a workflow), so the
    server applies its default retry policy of 3 retries. Supplying one failure
    would leave the retry SCHEDULED forever and the run RUNNING.
    """
    mocks = campaign_mocks()
    mocks["campaign_tests"] = [{"status": "FAILED", "output": {}} for _ in range(4)]
    execution = campaign(campaign_input(), mocks)
    require_status(execution, "COMPLETED")
    require_output(execution, outcome="incomplete")
    require_absent(execution, "tests_outcome_passed")


# -- PR approval and publication ------------------------------------------
def scenario_campaign_pr_approval_asked_on_every_run() -> None:
    """pr_draft_approval is unconditional: no input can route around it.

    campaign_input() carries no publication flag at all -- the human's answer at
    the gate is the whole publication decision -- so an approved draft is what
    pushes the branch and opens the PR.
    """
    execution = campaign(campaign_input(), campaign_mocks())
    require_status(execution, "COMPLETED")
    require_output(execution, prApprovalState="approved", publicationState="published",
                   prNumber=42)
    if "campaign_pr_approval" not in refs(execution):
        raise AssertionError("the campaign did not reach its PR draft approval gate")


def scenario_campaign_pr_approval_rejected() -> None:
    """A draft a human did not approve must push nothing and open nothing.

    This is also the only way a campaign now retains its branch locally, so the
    reported publicationState is the approval state itself rather than a generic
    label -- the reason the branch stayed local is the human's own decision.
    """
    mocks = campaign_mocks()
    mocks["campaign_pr_approval"] = mock({
        "approvalState": "suppressed", "branch": CAMPAIGN_BRANCH,
        "workspacePath": CAMPAIGN_WORKTREE, "candidateCommit": CAMPAIGN_COMMIT,
        "title": "", "body": ""})
    execution = campaign(campaign_input(), mocks)
    require_status(execution, "COMPLETED")
    require_output(execution, prApprovalState="suppressed", publicationState="suppressed",
                   pushed=False, outcome="verified")
    require_absent(execution, "campaign_push", "campaign_pr")


def scenario_campaign_push_blocked() -> None:
    """A blocked push must not be reported as a publication."""
    mocks = campaign_mocks()
    mocks["campaign_push"] = mock({"pushed": False, "head": CAMPAIGN_COMMIT})
    execution = campaign(campaign_input(), mocks)
    require_status(execution, "COMPLETED")
    require_absent(execution, "campaign_pr")
    if "campaign_capture_push_blocked" not in refs(execution):
        raise AssertionError("blocked push did not reach campaign_capture_push_blocked")


# -- in-place mode and cleanup --------------------------------------------
def scenario_campaign_inplace_checkpoint() -> None:
    """In-place runs commit a checkpoint in the operator's own checkout."""
    execution = campaign(campaign_input(inPlace=True), campaign_mocks(in_place=True))
    require_status(execution, "COMPLETED")
    require_output(execution, inPlace=True, publicationState="published")
    require_task_input(execution, "campaign_workspace", inPlace=True)
    if "campaign_inplace_checkpoint" not in refs(execution):
        raise AssertionError("in-place campaign did not reach the checkpoint commit")


def scenario_campaign_worktree_cleanup() -> None:
    execution = campaign(campaign_input(keepWorktree=False), campaign_mocks())
    require_status(execution, "COMPLETED")
    require_task_input(execution, "campaign_workspace_cleanup", keepWorktree=False,
                       outcome="verified")


def campaign_switch_branches() -> dict[str, set[str]]:
    """Every declared branch of every SWITCH in feature_campaign, default included."""
    branches: dict[str, set[str]] = {}

    def walk(tasks) -> None:
        for task in tasks:
            if task.get("type") == "SWITCH":
                branches[task["taskReferenceName"]] = (
                    set((task.get("decisionCases") or {}).keys()) | {DEFAULT_BRANCH})
            for key in ("decisionCases", "defaultCase", "forkTasks", "loopOver", "defaultTask"):
                value = task.get(key)
                if isinstance(value, dict):
                    for nested in value.values():
                        walk(nested)
                elif isinstance(value, list):
                    for item in value:
                        walk(item if isinstance(item, list) else [item])

    walk(definition("feature_campaign", 1).get("tasks") or [])
    return branches


DEFAULT_BRANCH = "<default>"


def scenario_campaign_every_gate_branch_is_covered() -> None:
    """Hold the campaign_* scenarios to complete branch coverage of every gate.

    feature_campaign routes entirely through SWITCH tasks -- no forks, no loops
    at this level -- so "every path works" means every branch of every gate is
    taken by some scenario. Asserting that here is what keeps the claim true as
    the definition grows: a new gate, or a new case on an existing one, fails
    this until a scenario drives it.

    A SWITCH reports the value it evaluated in `selectedCase`, so a value that
    matches no declared case is how the server says it took the default.
    """
    declared = campaign_switch_branches()
    taken: dict[str, set[str]] = {}
    scenarios = [value for name, value in sorted(globals().items())
                 if name.startswith("scenario_campaign_")
                 and name != "scenario_campaign_every_gate_branch_is_covered"]

    real_execute = globals()["execute"]

    def recording(name, version, inputs, mocks):
        result = real_execute(name, version, inputs, mocks)
        if name == "feature_campaign":
            for task in result.get("tasks") or []:
                if task.get("taskType") != "SWITCH":
                    continue
                ref = task["referenceTaskName"]
                selected = (task.get("outputData") or {}).get("selectedCase") or ""
                branch = selected if selected in declared.get(ref, set()) else DEFAULT_BRANCH
                taken.setdefault(ref, set()).add(branch)
        return result

    globals()["execute"] = recording
    try:
        for scenario in scenarios:
            scenario()
    finally:
        globals()["execute"] = real_execute

    uncovered = {gate: sorted(cases - taken.get(gate, set()))
                 for gate, cases in declared.items()
                 if cases - taken.get(gate, set())}
    if uncovered:
        raise AssertionError(
            f"{sum(len(v) for v in uncovered.values())} SWITCH branch(es) of "
            f"feature_campaign are never taken by any scenario: {uncovered}")


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
    scenario_campaign_clone_from_repo_url,
    scenario_campaign_local_repo_path,
    scenario_campaign_inherited_workspace,
    scenario_campaign_issue_driven,
    scenario_campaign_instruction_driven,
    scenario_campaign_rejects_neither_instruction_nor_issue,
    scenario_campaign_imported_plan_adopted,
    scenario_campaign_design_stop,
    scenario_campaign_design_limit_reached,
    scenario_campaign_plan_stop,
    scenario_campaign_plan_limit_reached,
    scenario_campaign_implementation_incomplete,
    scenario_campaign_final_verified,
    scenario_campaign_final_limit_reached,
    scenario_campaign_tests_failed,
    scenario_campaign_tests_subworkflow_failed,
    scenario_campaign_pr_approval_asked_on_every_run,
    scenario_campaign_pr_approval_rejected,
    scenario_campaign_push_blocked,
    scenario_campaign_inplace_checkpoint,
    scenario_campaign_worktree_cleanup,
    scenario_campaign_every_gate_branch_is_covered,
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
