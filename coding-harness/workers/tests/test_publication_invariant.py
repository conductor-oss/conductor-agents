"""The delivery invariant: issue_to_pr always routes its best branch to a PR.

A run that cannot be certified is still delivered as a draft PR.  Only the
authoritative ``passed`` delivery outcome may create or promote a ready PR.

These are structural assertions over the workflow definition rather than jq
evaluation, so they hold without shelling out to a jq binary.
"""

from __future__ import annotations

import json
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "issue_to_pr.json"


def _load() -> dict:
    return json.loads(WORKFLOW.read_text())


def _walk(tasks: list) -> list:
    """Flatten every task, including those nested in switch cases and loops."""
    flat = []
    for task in tasks:
        flat.append(task)
        cases = task.get("decisionCases") or {}
        for branch in cases.values():
            flat.extend(_walk(branch))
        flat.extend(_walk(task.get("defaultCase") or []))
        flat.extend(_walk(task.get("loopOver") or []))
    return flat


def _by_ref(ref: str) -> dict:
    for task in _walk(_load()["tasks"]):
        if task.get("taskReferenceName") == ref:
            return task
    raise AssertionError(f"task {ref!r} is missing from issue_to_pr")


def test_publication_plan_always_publishes_and_only_passed_is_ready():
    from common import issue_to_pr

    task = _by_ref("select_publication_plan")
    assert task["name"] == "issue_to_pr_publication_plan"
    assert issue_to_pr.resolve_publication_plan("passed") == \
        {"publish": "true", "draft": False, "reason": "", "agentAuthoredTest": False}
    blocked = issue_to_pr.resolve_publication_plan("incomplete_delivery")
    assert blocked["publish"] == "true"
    assert blocked["draft"] is True
    assert blocked["reason"] == "delivery outcome: incomplete_delivery"

    authored = issue_to_pr.resolve_publication_plan("passed", agent_authored_test=True)
    assert authored["draft"] is True
    assert authored["reason"] == "agent-authored test"
    assert authored["agentAuthoredTest"] is True


def test_push_and_pr_create_are_unconditional_top_level_routes():
    refs = [task["taskReferenceName"] for task in _load()["tasks"]]
    assert refs.index("push") < refs.index("pr") < refs.index("capture_publication")
    assert _by_ref("push")["optional"] is True
    pr = _by_ref("pr")
    assert pr["optional"] is True
    assert pr["inputParameters"]["draft"] == "${workflow.variables.prDraft}"
    assert pr["inputParameters"]["summary"] == "${workflow.variables.currentSummary}"
    assert pr["inputParameters"]["issueBody"] == "${issue.output.body}"


def test_draft_body_does_not_add_a_warning_section():
    # The body reaches pr_create untouched now, so the only way a warning could
    # appear is if something added it to the variable itself.
    body = _by_ref("pr")["inputParameters"]["body"]
    assert body == "${workflow.variables.currentBody}"
    assert "WARNING" not in json.dumps(_load())


def test_parent_owns_publishable_workspace_before_optional_child():
    refs = [task["taskReferenceName"] for task in _load()["tasks"]]
    assert refs.index("publication_workspace") < refs.index("cp")
    child = _by_ref("cp")
    assert child["optional"] is True
    assert child["inputParameters"]["workspacePath"] == \
        "${publication_workspace.output.worktreePath}"


def test_delivery_metadata_is_exposed_without_polluting_pr_body():
    output = _load()["outputParameters"]
    for key in ("deliveryOutcome", "deliveryRepairAttempts", "deliveryReports",
                "missingPaths", "rejectedPaths", "commandFailures", "sandboxDenials"):
        assert key in output
    body = _by_ref("pr")["inputParameters"]["body"]
    assert "missingPaths" not in body and "deliveryOutcome" not in body
