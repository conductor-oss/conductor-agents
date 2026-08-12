from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


def _walk(value):
    if isinstance(value, dict):
        if "taskReferenceName" in value:
            yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _task(workflow: dict, reference: str) -> dict:
    return next(task for task in _walk(workflow) if task["taskReferenceName"] == reference)


def _load(name: str) -> dict:
    return json.loads((WORKFLOWS / f"{name}.json").read_text(encoding="utf-8"))


def test_pr_review_clones_upstream_and_resolves_the_pr_against_it():
    workflow = _load("pr_review")
    assert _task(workflow, "clone")["inputParameters"]["repoUrl"] == "${workflow.input.repo}"
    checkout = _task(workflow, "co")["inputParameters"]
    assert checkout["repo"] == "${workflow.input.repo}"


def test_pr_review_has_bounded_private_investigation_and_direct_publication_decisions():
    workflow = _load("pr_review")
    assert workflow["version"] == 1
    assert workflow["inputTemplate"]["reviewGuidance"] == ""
    assert workflow["inputTemplate"]["maxInvestigationPasses"] == 5
    assert "approvalMode" in workflow["inputParameters"]
    # The gate policy is a worker now: three one-line jq tasks used to clamp the
    # investigation budget, check the remaining passes, and route approve/human.
    gate = _task(workflow, "select_review_gate")
    assert gate["name"] == "review_gate_policy"
    assert gate["inputParameters"]["approvalMode"] == "${workflow.input.approvalMode}"
    bound = _task(workflow, "bound_investigation_limit")["inputParameters"]
    assert bound["requested"] == "${workflow.input.maxInvestigationPasses}"
    # The clamp is a worker now, so it is asserted against behaviour rather
    # than against the shape of a jq expression.
    from gitops.tasks import review_gate_policy
    from conductor.client.http.models.task import Task

    def limit(requested):
        task = Task(); task.task_id = "t"; task.workflow_instance_id = "w"
        task.input_data = {"requested": requested}
        return review_gate_policy(task).output_data["limit"]

    assert limit(9) == 5 and limit(-1) == 0 and limit(3) == 3
    # A non-numeric budget must not become an unbounded loop.
    assert limit("many") == 5 and limit(None) == 5 and limit(True) == 5
    loop = _task(workflow, "review_investigation_loop")
    assert loop["type"] == "DO_WHILE"
    assert loop["evaluatorType"] == "graaljs"
    assert "$.review_investigation_loop['iteration'] <= $.maxInvestigationPasses" in \
        loop["loopCondition"]
    assert loop["inputParameters"]["review_investigation_loop"] == \
        "${review_investigation_loop.output}"
    assert loop["inputParameters"]["maxInvestigationPasses"] == \
        "${bound_investigation_limit.output.limit}"
    availability = _task(workflow, "investigation_availability")
    # The available-action set is a worker decision now.
    from gitops.tasks import review_gate_policy
    from conductor.client.http.models.task import Task

    def _actions(count, limit):
        task = Task(); task.task_id = "t"; task.workflow_instance_id = "w"
        task.input_data = {"count": count, "requested": limit}
        return review_gate_policy(task).output_data

    assert "investigate" in _actions(0, 5)["actions"]
    # Once the budget is spent the option disappears rather than looping on.
    assert "investigate" not in _actions(5, 5)["actions"]
    assert _actions(5, 5)["canInvestigate"] is False
    gate = _task(workflow, "review_gate")["inputParameters"]
    assert gate["availableActions"] == "${investigation_availability.output.actions}"
    assert gate["draft"]["investigationHistory"] == "${workflow.variables.investigationHistory}"
    investigation = _task(workflow, "review_investigation")
    assert investigation["optional"] is True
    assert investigation["inputParameters"]["failSoft"] is True
    assert investigation["inputParameters"]["tools"] == ["Read", "Grep", "Glob"]
    assert investigation["inputParameters"]["resumeSessionId"] == \
        "${workflow.variables.reviewSessionId}"
    assert investigation["inputParameters"]["schema"]["required"] == ["answer", "review"]
    normalize = _task(workflow, "normalize_investigation")
    assert normalize["name"] == "pr_review_investigation"
    assert normalize["inputParameters"]["priorSessionId"] == "${workflow.variables.reviewSessionId}"
    refs = {task["taskReferenceName"] for task in _walk(workflow)}
    assert "revise_review" not in refs
    request = _task(workflow, "request_review_changes")["inputParameters"]
    assert request["event"] == "REQUEST_CHANGES"
    # A human "revise" decision must not silently discard the AI's own
    # findings -- confirmed live (PR #1480, review 4903273554): the prior
    # behavior posted only the reviewer's raw feedback text as the entire
    # public GitHub review body, with comments hardcoded to []. The summary
    # now carries the AI's review alongside the reviewer's note, and the
    # AI's own inline comments are preserved rather than dropped.
    assert request["currentReview"]["summary"] == (
        "${normalize_review_decision.output.review.summary}"
        "\n\n---\nReviewer note (changes requested):\n"
        "${normalize_review_decision.output.feedback}"
    )
    assert request["currentReview"]["comments"] == \
        "${normalize_review_decision.output.review.comments}"
    assert _task(workflow, "submit")["inputParameters"]["structured"] == \
        "${workflow.variables.currentReview}"
    assert _task(workflow, "submit")["inputParameters"]["event"] == \
        "${workflow.variables.event}"
    review_task = _task(workflow, "normalize_review")
    assert review_task["name"] == "pr_review_summary"
    from common import gate_decision
    assert gate_decision.summarize_review({"comments": []})["summary"] == "LGTM"
    assert workflow["outputParameters"]["investigationHistory"] == \
        "${workflow.variables.investigationHistory}"
    rendered = json.dumps(workflow)
    assert rendered.index('"taskReferenceName": "review_investigation"') < \
        rendered.index('"taskReferenceName": "review_publication_gate"')
    assert rendered.index('"taskReferenceName": "review_publication_gate"') < \
        rendered.index('"taskReferenceName": "submit"')


def test_pr_review_investigation_normalizer_executes_success_and_preserves_session_on_failure():
    from common import pr_review as pr_review_logic

    base = dict(question="Trace callers",
               prior_review={"summary": "LGTM", "verdict": "approve", "comments": []},
               history=[], prior_tokens=10, prior_cost=1.0, prior_session_id="session-1")

    succeeded = pr_review_logic.normalize_investigation(
        **base,
        structured={"answer": "Both callers are safe.",
                   "review": {"summary": "ignored", "verdict": "request_changes", "comments": []}},
        status="success", error="", session_id="session-2", tokens=4, cost=0.5)
    assert succeeded["review"] == {"summary": "LGTM", "verdict": "approve", "comments": []}
    assert succeeded["sessionId"] == "session-2"
    assert succeeded["history"][0]["answer"] == "Both callers are safe."
    assert succeeded["count"] == 1 and succeeded["tokenUsed"] == 14 and succeeded["costUsd"] == 1.5

    failed = pr_review_logic.normalize_investigation(
        **base, structured={}, status="failed", error="backend unavailable",
        session_id="", tokens=None, cost=None)
    assert failed["review"] == base["prior_review"]
    assert failed["sessionId"] == "session-1"
    assert failed["history"][0]["status"] == "failed"


def test_address_pr_preserves_fork_origin_but_uses_upstream_pr_metadata():
    workflow = _load("address_pr")
    assert _task(workflow, "clone")["inputParameters"]["repoUrl"] == "${fb.output.headRepoUrl}"
    checkout = _task(workflow, "co")["inputParameters"]
    assert checkout["repo"] == "${workflow.input.repo}"
    assert checkout["branch"] == "${fb.output.head}"
    publish = _task(workflow, "publish")["inputParameters"]
    assert publish["headRepo"] == "${fb.output.headRepo}"
    assert publish["repo"] == "${workflow.input.repo}"


def test_address_pr_requires_approval_before_publishing_changes():
    # address_gate itself lives in address_pr_approval.json now (extracted to
    # keep address_pr's own graph small); the parent only needs to call that
    # sub-workflow before publish -- see test_address_pr_approval_gate below
    # for the WAIT task's own properties.
    workflow = _load("address_pr")
    approval = _task(workflow, "approval")
    publish = _task(workflow, "publish")
    assert approval["subWorkflowParam"] == {"name": "address_pr_approval", "version": 1}
    assert publish["subWorkflowParam"] == {"name": "publish_verified_pr", "version": 1}
    # The parent owns approval; the shared child owns all remote mutations.
    rendered = json.dumps(workflow)
    assert rendered.index('"taskReferenceName": "approval"') < \
        rendered.index('"taskReferenceName": "publish"')


def test_a_revision_infra_failure_degrades_rather_than_failing_the_whole_run():
    # revise_address_candidate (code_parallel) is the one task in the approval
    # loop that runs real, unattended coding-agent work; an infra failure
    # there (not a verification failure -- a genuine task error) must degrade
    # to verification_blocked via the loop's own handling, not fail the whole
    # address_pr run the way an unmarked required task would.
    revise = _task(_load("address_pr_approval"), "revise_address_candidate")
    assert revise["type"] == "SUB_WORKFLOW"
    assert revise["subWorkflowParam"] == {"name": "code_parallel", "version": 1}
    assert revise.get("optional") is True


def test_address_pr_approval_gate_waits_for_a_human_before_anything_publishes():
    approval = _load("address_pr_approval")
    gate = _task(approval, "address_gate")
    assert gate["type"] == "WAIT"
    assert gate["inputParameters"]["branch"] == "${workflow.input.branch}"
    assert gate["inputParameters"]["availableActions"] == ["approve", "revise", "stop"]
    rendered = json.dumps(approval)
    assert rendered.index('"taskReferenceName": "address_gate"') < \
        rendered.index('"taskReferenceName": "revise_address_candidate"')


def test_address_pr_requires_independent_local_and_exact_sha_ci_verification():
    workflow = _load("address_pr")
    rendered = json.dumps(workflow)
    assert '"taskReferenceName": "verify"' in rendered
    assert rendered.index('"taskReferenceName": "verify"') < rendered.index('"taskReferenceName": "publish"')
    verify = _task(workflow, "verify")
    assert verify["subWorkflowParam"] == {"name": "test_cycle", "version": 1}
    assert verify["inputParameters"]["candidateCommit"] == \
        "${workflow.variables.currentCandidate}"
    # Every repair-loop iteration re-runs this gate, not just a final check --
    # a repository-wide suite here reruns the whole suite per iteration, which
    # is both expensive and, on a host that also runs a local Conductor
    # server, can spuriously collide with an SDK-repo's own test suite
    # defaulting to that same local server (see address_pr_repair, which
    # already used targeted). Scoped to the review-feedback fix's own diff.
    assert verify["inputParameters"]["testMode"] == "targeted"
    assert "allowHeavySuites" not in verify["inputParameters"]
    assert _task(workflow, "publish")["inputParameters"]["candidateCommit"] == \
        "${workflow.variables.currentCandidate}"

    publisher = _load("publish_verified_pr")
    published = json.dumps(publisher)
    assert published.index('"taskReferenceName": "candidate_guard"') < published.index('"taskReferenceName": "push"')
    assert published.index('"taskReferenceName": "branch_guard"') < published.index('"taskReferenceName": "push"')
    assert published.index('"taskReferenceName": "push"') < published.index('"taskReferenceName": "ci"')
    assert published.index('"taskReferenceName": "ci"') < published.index('"taskReferenceName": "reply"')


def test_failed_local_verification_uses_shared_bounded_remediation_and_hands_back_the_candidate():
    workflow = _load("address_pr")
    repair = _task(workflow, "repair")
    assert repair["type"] == "SUB_WORKFLOW"
    assert repair["subWorkflowParam"] == {"name": "address_pr_repair", "version": 1}

    recovery = _load("address_pr_repair")
    remediation = _task(recovery, "repair")
    assert remediation["type"] == "SUB_WORKFLOW"
    assert remediation["subWorkflowParam"] == {"name": "test_cycle", "version": 1}
    assert remediation["inputParameters"]["priorVerification"] == "${workflow.input.verification}"
    assert remediation["inputParameters"]["repairPromptTemplate"] == "${workflow.input.fixPromptTemplate}"
    task_types = {task["type"] for task in _walk(recovery)}
    task_names = {task["name"] for task in _walk(recovery)}
    assert "git_push" not in task_names
    assert "pr_comment" not in task_names
    assert "WAIT" not in task_types
    assert recovery["outputParameters"]["publicationState"] == "not_attempted"


def test_address_pr_does_not_inject_the_removed_verification_budget():
    workflow = _load("address_pr")
    parallel_calls = [
        task for task in _walk(workflow)
        if task.get("type") == "SUB_WORKFLOW"
        and task.get("subWorkflowParam", {}).get("name") == "code_parallel"
    ]
    assert parallel_calls
    assert all("verificationAttempts" not in task["inputParameters"]
               for task in parallel_calls)

    code_parallel = _load("code_parallel")
    refs = {task["taskReferenceName"] for task in _walk(code_parallel)}
    assert "plan_loop" in refs
    assert "verify_loop" not in refs
    assert "verify" not in refs


def test_remediation_retains_prior_candidate_if_optional_commit_has_no_sha():
    from common import test_plan

    workflow = _load("test_cycle")
    select = _task(workflow, "repair_result")
    assert select["name"] == "test_repair_record"
    assert select["inputParameters"]["currentCandidate"] == "${workflow.variables.candidateCommit}"
    assert select["inputParameters"]["commit"] == "${repair_commit.output}"

    # The candidate only advances on real evidence: a non-blank SHA, at least one
    # staged path, and an explicit non-no-op. Anything weaker keeps the old one.
    for commit in ({}, {"noOp": True, "stagedPaths": []},
                   {"noOp": False, "stagedPaths": [], "commit": "new"},
                   {"noOp": False, "stagedPaths": ["a"], "commit": "   "}):
        assert test_plan.record_repair(current_candidate="old", agent={},
                                       commit=commit)["candidate"] == "old"
    advanced = test_plan.record_repair(
        current_candidate="old", agent={},
        commit={"noOp": False, "stagedPaths": ["a.py"], "commit": "new"})
    assert advanced["candidate"] == "new"
    assert advanced["report"]["persisted"] is True


def test_remediation_stops_after_a_blocked_discovery_or_verifier_result():
    workflow = _load("test_cycle")
    loop = _task(workflow, "test_fix_loop")
    assert "$.executionOutcome === 'code_failed'" in loop["loopCondition"]
    assert "infra_blocked" not in loop["loopCondition"]


def test_shared_publish_path_keeps_parent_approval_branch_guard_and_exact_sha_ci_gate():
    parent = _load("address_pr")
    publish = _load("publish_verified_pr")
    parent_rendered = json.dumps(parent)
    rendered = json.dumps(publish)
    assert parent_rendered.index('"taskReferenceName": "approval"') < \
        parent_rendered.index('"taskReferenceName": "publish"')
    assert rendered.index('"taskReferenceName": "candidate_guard"') < rendered.index('"taskReferenceName": "push"')
    assert rendered.index('"taskReferenceName": "branch_guard"') < rendered.index('"taskReferenceName": "push"')
    assert rendered.index('"taskReferenceName": "push"') < rendered.index('"taskReferenceName": "ci"')
    assert '"taskReferenceName": "ci_poll"' in rendered
    assert _task(publish, "candidate_guard")["inputParameters"]["expectedHead"] == "${workflow.input.candidateCommit}"
    assert _task(publish, "push")["inputParameters"]["expectedHead"] == "${workflow.input.candidateCommit}"
    assert _task(publish, "branch_guard")["inputParameters"]["repo"] == "${workflow.input.headRepo}"
    assert _task(publish, "reply")["inputParameters"]["repo"] == "${workflow.input.repo}"


def test_every_other_automated_publication_is_bound_to_its_candidate_commit():
    demo = _load("github_demo")
    assert _task(demo, "branch")["inputParameters"]["forceNew"] is True
    assert _task(demo, "push")["inputParameters"]["expectedHead"] == \
        "${verify.output.candidateCommit}"
    assert _task(demo, "push")["inputParameters"]["branch"] == "${branch.output.branch}"
    assert _task(demo, "pr")["inputParameters"]["head"] == "${branch.output.branch}"
    assert demo["outputParameters"]["branch"] == "${branch.output.branch}"
    issue = _load("issue_to_pr")
    assert _task(issue, "push")["inputParameters"]["expectedHead"] == \
        "${workflow.variables.candidateCommit}"
    assert _task(issue, "push")["inputParameters"]["branch"] == \
        "${workflow.variables.currentBranch}"
    assert _task(issue, "pr")["inputParameters"]["head"] == \
        "${workflow.variables.currentBranch}"
    assert issue["outputParameters"]["changeBranch"] == "${workflow.variables.currentBranch}"
    assert _task(issue, "capture_initial_candidate")["inputParameters"]["currentBranch"] == \
        "${publication_workspace.output.branch}"
    # The gate used to read a jq task whose whole expression was the literal
    # "auto"; the constant now sits on the SWITCH itself.
    assert _task(issue, "approve_gate")["inputParameters"]["mode"] == "auto"
    from common import issue_to_pr as issue_to_pr_logic

    plan_task = _task(issue, "select_publication_plan")
    assert plan_task["name"] == "issue_to_pr_publication_plan"
    assert issue_to_pr_logic.resolve_publication_plan("code_failed")["draft"] is True
    assert _task(_load("openspec_development"), "openspec_push")["inputParameters"]["expectedHead"] == "${openspec_finalize.output.commit}"


def test_issue_pr_descriptions_only_compose_a_summary_section():
    """The one-section invariant is enforced in pr_description, not in jq.

    issue_to_pr used to strip markdown in jq and hand the result to pr_create,
    which ran format_summary over it again. Only the worker remains, and it is
    stronger: it also drops issue-template labels and placeholder prose, and it
    applies the 500-character cap the PR description policy requires.
    """
    from common import pr_description

    rendered = json.dumps(_load("issue_to_pr"))
    for retired in ("compose_pr_body", "compose_revised_pr_body", "compose_publication_body"):
        assert retired not in rendered

    body = pr_description.format_summary(
        Path(__file__).resolve().parents[1],
        "## Why\nFix the thing.\n\nCloses #12\n<!-- comment -->",
    )["body"]
    assert body.startswith("## Summary")
    for forbidden in ("## Subtasks", "## Verification", "<!--"):
        assert forbidden not in body
    assert body.count("## ") == 1


def test_parallel_address_verifies_and_publishes_from_the_handoff_workspace():
    workflow = _load("address_pr")
    initial = _task(workflow, "cp")["inputParameters"]
    assert initial["repoPath"] == "${clone.output.repoPath}"
    assert initial["workspacePath"] == "${clone.output.repoPath}"
    capture = _task(workflow, "parallel_candidate")["inputParameters"]
    assert capture["currentWorkspacePath"] == "${cp.output.sourceHandoff.repoPath}"
    approval = _task(workflow, "approval")["inputParameters"]
    assert approval["repoPath"] == "${clone.output.repoPath}"
    assert approval["workspacePath"] == "${workflow.variables.currentWorkspacePath}"
    assert _task(workflow, "verify")["inputParameters"]["repoPath"] == \
        "${workflow.variables.currentWorkspacePath}"
    assert _task(workflow, "publish")["inputParameters"]["repoPath"] == \
        "${workflow.variables.currentWorkspacePath}"

    # revise_address_candidate itself now lives in address_pr_approval.json,
    # reusing the same handoff workspace forwarded as an input.
    revision = _task(_load("address_pr_approval"), "revise_address_candidate")["inputParameters"]
    assert revision["repoPath"] == "${workflow.input.repoPath}"
    assert revision["workspacePath"] == "${workflow.input.workspacePath}"
