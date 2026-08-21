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


def test_pr_draft_approval_gate_waits_for_a_human_before_anything_publishes():
    approval = _load("pr_draft_approval")
    gate = _task(approval, "pr_gate")
    assert gate["type"] == "WAIT"
    assert gate["inputParameters"]["workflow"] == "${workflow.input.callerWorkflow}"
    assert gate["inputParameters"]["availableActions"] == \
        ["approve", "revise", "stop", "later"]
    assert gate["inputParameters"]["draft"]["title"] == "${workflow.variables.currentTitle}"
    rendered = json.dumps(approval)
    assert rendered.index('"taskReferenceName": "pr_gate"') < \
        rendered.index('"taskReferenceName": "revise_candidate"')
    revise = _task(approval, "revise_candidate")
    assert revise["subWorkflowParam"] == {"name": "code_parallel", "version": 1}
    assert "${workflow.input.originalContext}" in revise["inputParameters"]["instruction"]
    assert "${normalize_pr_decision.output.feedback}" in revise["inputParameters"]["instruction"]


def test_design_docs_callers_all_identify_themselves_by_name():
    # Confirmed live (via the address_pr_approval precedent): a gate living
    # inside a sub-workflow reports Conductor's own workflowType as that
    # sub-workflow's name, not the logical top-level caller's -- design_docs
    # is now reachable up to two levels deep (issue_to_pr -> code_parallel ->
    # design_docs), so every hop must forward its own real identity rather
    # than letting design_docs fall back to reporting its own name.
    design_docs = _load("design_docs")
    gate = _task(design_docs, "design_review")
    assert gate["inputParameters"]["workflow"] == "${workflow.input.callerWorkflow}"
    assert "callerWorkflow" in design_docs["inputSchema"]["data"]["required"]

    code_parallel = _load("code_parallel")
    assert code_parallel["inputTemplate"]["callerWorkflow"] == "code_parallel"
    cp_design_call = _task(code_parallel, "design")
    assert cp_design_call["inputParameters"]["callerWorkflow"] == "${workflow.input.callerWorkflow}"

    campaign = _load("feature_campaign")
    campaign_design_call = _task(campaign, "campaign_design_docs")
    assert campaign_design_call["inputParameters"]["callerWorkflow"] == "feature_campaign"

    for wf_name in ("issue_to_pr", "address_pr"):
        workflow = _load(wf_name)
        cp_call = _task(workflow, "cp")
        assert cp_call["subWorkflowParam"]["name"] == "code_parallel"
        assert cp_call["inputParameters"]["callerWorkflow"] == wf_name, wf_name


def test_issue_to_pr_and_feature_campaign_both_reach_pr_draft_approval():
    # The whole point of the extraction: prove it's genuinely reusable, not
    # just moved out of issue_to_pr into a differently-shaped dead end.
    for wf_name in ("issue_to_pr", "feature_campaign"):
        workflow = _load(wf_name)
        call = next(node for node in _walk(workflow)
                   if node.get("type") == "SUB_WORKFLOW"
                   and node.get("subWorkflowParam", {}).get("name") == "pr_draft_approval")
        assert call["inputParameters"]["callerWorkflow"] == wf_name, wf_name
        assert call["inputParameters"]["candidateCommit"], wf_name


def test_address_pr_requires_local_verification_then_publishes_without_waiting_on_ci():
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
    # Publication completes once the branch is updated -- no CI wait/poll.
    assert published.index('"taskReferenceName": "push"') < published.index('"taskReferenceName": "reply"')
    assert '"taskReferenceName": "ci"' not in published
    assert '"taskReferenceName": "ci_poll"' not in published


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


def test_shared_publish_path_keeps_parent_approval_and_branch_guard():
    parent = _load("address_pr")
    publish = _load("publish_verified_pr")
    parent_rendered = json.dumps(parent)
    rendered = json.dumps(publish)
    assert parent_rendered.index('"taskReferenceName": "approval"') < \
        parent_rendered.index('"taskReferenceName": "publish"')
    assert rendered.index('"taskReferenceName": "candidate_guard"') < rendered.index('"taskReferenceName": "push"')
    assert rendered.index('"taskReferenceName": "branch_guard"') < rendered.index('"taskReferenceName": "push"')
    # Completes once the push/comment land -- no CI wait/poll (see
    # test_publish_completes_without_waiting_on_ci below for why).
    assert '"taskReferenceName": "ci"' not in rendered
    assert '"taskReferenceName": "ci_poll"' not in rendered
    assert _task(publish, "candidate_guard")["inputParameters"]["expectedHead"] == "${workflow.input.candidateCommit}"
    # push is still bound to an exact SHA -- just one resolved through
    # workflow.variables.pushExpectedHead now, since a reconciled (and, for a
    # real conflict, re-verified) branch-drift commit differs from the
    # original candidateCommit. See test_publish_reconciles_branch_drift_*
    # below for exactly what populates pushExpectedHead in each case.
    assert _task(publish, "push")["inputParameters"]["expectedHead"] == "${workflow.variables.pushExpectedHead}"
    assert _task(publish, "branch_guard")["inputParameters"]["repo"] == "${workflow.input.headRepo}"
    assert _task(publish, "reply")["inputParameters"]["repo"] == "${workflow.input.repo}"


def test_publish_completes_without_waiting_on_ci():
    # Dropped deliberately: this workflow's job is publishing the verified
    # candidate, not monitoring CI -- GitHub's own PR page already reports
    # check status independently, and nothing downstream (address_pr's own
    # output) used the old ciVerificationState for anything but display.
    publish = _load("publish_verified_pr")
    gate = next(x for x in _walk(publish) if x.get("taskReferenceName") == "push_result_gate")
    assert gate["type"] == "SWITCH"
    assert gate["inputParameters"]["pushed"] == "${push.output.pushed}"
    published = gate["decisionCases"]["true"]
    assert [t["taskReferenceName"] for t in published] == ["reply", "capture_publication_success"]
    success = next(t for t in published if t["taskReferenceName"] == "capture_publication_success")
    assert success["inputParameters"]["publicationState"] == "published"
    blocked = next(t for t in gate["defaultCase"] if t["taskReferenceName"] == "capture_push_blocked")
    assert blocked["inputParameters"]["publicationState"] == "${push.output.publicationState}"
    assert blocked["inputParameters"]["pushed"] is False

    rendered = json.dumps(publish)
    for removed in ("ciState", "ci_verification_gate", "ci_poll_final", "pr_commit_checks"):
        assert removed not in rendered

    address = _load("address_pr")
    assert "ciVerificationState" not in json.dumps(address)


def test_publish_reconciles_branch_drift_instead_of_just_reporting_it():
    # A non-force push already refuses a moved remote branch on its own (git's
    # own fast-forward protection) -- branch_guard_gate's job is deciding what
    # to do about that, not re-implementing the refusal. Confirmed live
    # (execution 9ae65177...): reporting "branch_drift" and giving up wasted
    # a fully-verified candidate whenever the PR branch moved during a run.
    publish = _load("publish_verified_pr")
    matched = next(x for x in _walk(publish) if x.get("taskReferenceName") == "branch_guard_gate")["decisionCases"]["matched"]
    ready = next(t for t in matched if t["taskReferenceName"] == "capture_ready_to_push")
    assert ready["inputParameters"]["readyToPush"] is True
    assert ready["inputParameters"]["pushExpectedHead"] == "${workflow.input.candidateCommit}"

    drifted = next(x for x in _walk(publish) if x.get("taskReferenceName") == "branch_guard_gate")["defaultCase"]
    reconcile = next(t for t in drifted if t["taskReferenceName"] == "reconcile")
    assert reconcile["name"] == "reconcile_branch_drift"

    gate = next(t for t in drifted if t["taskReferenceName"] == "reconcile_gate")
    # A clean merge (the remote's new commits never touched the candidate's
    # own changed files) is trusted without re-verification.
    merged = next(t for t in gate["decisionCases"]["merged"] if t["taskReferenceName"] == "capture_reconciled_merge")
    assert merged["inputParameters"]["readyToPush"] is True
    assert merged["inputParameters"]["pushExpectedHead"] == "${reconcile.output.commit}"

    # A resolved conflict is genuinely new content and must be re-verified
    # before it is ever trusted for publication.
    resolved = gate["decisionCases"]["resolved"]
    revalidate = next(t for t in resolved if t["taskReferenceName"] == "revalidate_reconciled_candidate")
    assert revalidate["type"] == "SUB_WORKFLOW"
    assert revalidate["subWorkflowParam"] == {"name": "test_cycle", "version": 1}
    assert revalidate["inputParameters"]["candidateCommit"] == "${reconcile.output.commit}"
    verify_gate = next(t for t in resolved if t["taskReferenceName"] == "reconcile_verify_gate")
    assert verify_gate["inputParameters"]["state"] == "${revalidate_reconciled_candidate.output.testsPassed}"
    verified = next(t for t in verify_gate["decisionCases"]["true"]
                    if t["taskReferenceName"] == "capture_reconciled_verified")
    assert verified["inputParameters"]["readyToPush"] is True
    unverified = next(t for t in verify_gate["defaultCase"]
                      if t["taskReferenceName"] == "capture_reconcile_verification_failed")
    assert unverified["inputParameters"]["readyToPush"] is False
    assert unverified["inputParameters"]["publicationState"] == "conflict_verification_failed"

    # Resolution failing outright (not even a conflict to re-verify) is the
    # only remaining case that still gives up -- nothing to push exists yet.
    conflicted = next(t for t in gate["defaultCase"] if t["taskReferenceName"] == "capture_branch_drift")
    assert conflicted["inputParameters"]["readyToPush"] is False
    assert conflicted["inputParameters"]["publicationState"] == "branch_drift"

    # push only ever runs when something was actually deemed trustworthy.
    push_gate = next(x for x in _walk(publish) if x.get("taskReferenceName") == "push_ready_gate")
    assert push_gate["inputParameters"]["ready"] == "${workflow.variables.readyToPush}"
    assert any(t["taskReferenceName"] == "push" for t in push_gate["decisionCases"]["true"])


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
    # approvePr was "reserved for future manual-approval wiring" but never
    # actually read -- the gate's mode was hardcoded to the literal "auto",
    # making pr_approval_loop permanently dead code. Confirmed live and fixed:
    # the gate now reads the real input, and the loop itself is extracted
    # into pr_draft_approval so other workflows (feature_campaign) can reuse it.
    gate = _task(issue, "approve_gate")
    assert gate["inputParameters"]["approvePr"] == "${workflow.input.approvePr}"
    assert set(gate["decisionCases"].keys()) == {"true", "false"}
    approval = gate["decisionCases"]["true"][0]
    assert approval["type"] == "SUB_WORKFLOW"
    assert approval["subWorkflowParam"] == {"name": "pr_draft_approval", "version": 1}
    assert approval["inputParameters"]["callerWorkflow"] == "issue_to_pr"
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
