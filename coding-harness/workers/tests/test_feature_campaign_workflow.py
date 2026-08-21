from __future__ import annotations

import json
from pathlib import Path


WF = Path(__file__).resolve().parents[1] / "workflows"


def _load(name):
    return json.loads((WF / f"{name}.json").read_text())


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_feature_campaign_contract_and_bounded_loops():
    wf = _load("feature_campaign")
    assert wf["version"] == 1 and wf["schemaVersion"] == 2
    assert set(wf["inputParameters"]) >= {"repoPath", "instruction"}
    defaults = wf["inputTemplate"]
    assert defaults["maxTurns"] == 500 and defaults["maxBudgetUsd"] == 50.0
    assert defaults["maxTasks"] == 25 and defaults["maxParallelism"] == 6
    assert defaults["maxWaves"] == 20
    # design_loop, plan_loop, implementation_loop, and final_loop all moved
    # out: design is now the shared design_docs sub-workflow, plan the shared
    # dag_plan_approval sub-workflow, implementation the shared
    # implementation_waves sub-workflow, and final the shared
    # final_verification sub-workflow (see
    # test_campaign_design_phase_delegates_to_design_docs,
    # test_campaign_plan_phase_delegates_to_dag_plan_approval,
    # test_campaign_implementation_phase_delegates_to_implementation_waves,
    # and test_campaign_final_phase_delegates_to_final_verification), leaving
    # this workflow with no bounded DO_WHILE loops of its own -- every
    # revision loop now lives in an extracted, independently-testable
    # sub-workflow.
    loops = [x for x in _walk(wf) if x.get("type") == "DO_WHILE"]
    assert loops == []


def test_campaign_can_be_driven_by_a_github_issue_instead_of_instruction():
    # feature_campaign previously only accepted free-text `instruction` --
    # unlike issue_to_pr, it had no way to be driven directly by a GitHub
    # issue. Mirrors issue_to_pr's issue_fetch pattern but keeps "what to
    # build" (instruction vs issueNumber) orthogonal to "where to work"
    # (repo vs repoPath), since an issue can drive work against either source.
    wf = _load("feature_campaign")
    assert "issueNumber" in wf["inputParameters"]
    assert wf["inputTemplate"]["issueNumber"] == 0
    assert wf["inputTemplate"]["instruction"] == ""
    assert "instruction" not in wf["inputSchema"]["data"]["required"]
    assert "issueNumber" not in wf["inputSchema"]["data"]["required"]

    gate = next(x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_issue_gate")
    assert gate["type"] == "SWITCH"
    assert gate["inputParameters"]["issueNumber"] == "${workflow.input.issueNumber}"

    issue_case = gate["decisionCases"]["issue"]
    fetch = next(t for t in issue_case if t["taskReferenceName"] == "campaign_issue")
    assert fetch["name"] == "issue_fetch"
    assert fetch["inputParameters"]["repo"] == "${workflow.input.repo}"
    assert fetch["inputParameters"]["number"] == "${workflow.input.issueNumber}"

    set_from_issue = next(t for t in issue_case if t["taskReferenceName"] == "campaign_issue_instruction")
    assert set_from_issue["type"] == "SET_VARIABLE"
    assert "${campaign_issue.output.title}" in set_from_issue["inputParameters"]["effectiveInstruction"]
    assert "${campaign_issue.output.body}" in set_from_issue["inputParameters"]["effectiveInstruction"]
    assert "${campaign_issue.output.linkedContext}" in set_from_issue["inputParameters"]["effectiveInstruction"]
    assert set_from_issue["inputParameters"]["issueTitle"] == "${campaign_issue.output.title}"

    default_case = gate["defaultCase"]
    set_local = next(t for t in default_case if t["taskReferenceName"] == "campaign_local_instruction")
    assert set_local["inputParameters"]["effectiveInstruction"] == "${workflow.input.instruction}"
    assert set_local["inputParameters"]["issueTitle"] == ""

    # Every downstream prompt/summary must read the resolved variable, not
    # the raw input directly -- otherwise issue-driven runs would silently
    # fall back to an empty instruction.
    design_docs_call = next(x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_design_docs")
    assert design_docs_call["inputParameters"]["instruction"] == "${workflow.variables.effectiveInstruction}"
    plan_approval_call = next(x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_plan_approval")
    assert plan_approval_call["inputParameters"]["instruction"] == "${workflow.variables.effectiveInstruction}"
    final = next(x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_final_verification")
    assert final["inputParameters"]["instruction"] == "${workflow.variables.effectiveInstruction}"
    pr = next(x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_pr")
    assert pr["inputParameters"]["summaryFallback"] == "${workflow.variables.effectiveInstruction}"

    assert wf["outputParameters"]["issueNumber"] == "${workflow.input.issueNumber}"
    assert wf["outputParameters"]["issueTitle"] == "${workflow.variables.issueTitle}"
    assert wf["outputParameters"]["issueUrl"] == "${workflow.variables.issueUrl}"


def test_campaign_defaults_rejects_neither_instruction_nor_issue_given():
    wf = _load("feature_campaign")
    defaults = next(x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_defaults")
    assert defaults["type"] == "INLINE"
    assert "throw new Error" in defaults["inputParameters"]["expression"]
    assert defaults["inputParameters"]["instruction"] == "${workflow.input.instruction}"
    assert defaults["inputParameters"]["issueNumber"] == "${workflow.input.issueNumber}"


def test_campaign_pr_closes_the_driving_issue_when_one_was_given():
    wf = _load("feature_campaign")
    defaults = next(x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_defaults")
    assert "closesLine" in defaults["inputParameters"]["expression"]
    # pr_create itself reads the resolved publishBody variable (set either by
    # skip_campaign_pr_approval directly, or by a human-approved/revised
    # pr_draft_approval outcome) rather than composing the closes-line inline,
    # so the same text reaches GitHub whichever path produced it.
    skip = next(x for x in _walk(wf) if x.get("taskReferenceName") == "skip_campaign_pr_approval")
    assert skip["inputParameters"]["publishBody"] == (
        "${workflow.input.prBody}${campaign_publication_plan.output.agentAuthoredTestNote}"
        "${campaign_defaults.output.result.closesLine}"
    )
    approval = next(x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_pr_approval")
    assert approval["inputParameters"]["body"] == skip["inputParameters"]["publishBody"]
    pr = next(x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_pr")
    assert pr["inputParameters"]["body"] == "${workflow.variables.publishBody}"


def test_campaign_workspace_accepts_a_repo_to_clone_not_only_a_local_path():
    # Confirmed live: a caller with no existing local checkout put a repo URL
    # into repoPath (the only input feature_campaign exposed), which
    # git.validate_repo_path correctly rejects ("repoPath must be a local
    # filesystem path, not a repository URL") since workspace_prepare already
    # supports cloning via repoUrl into a fresh temp workspace -- it just had
    # no way to reach that path from this workflow's inputs.
    wf = _load("feature_campaign")
    assert "repo" in wf["inputParameters"]
    assert "repoPath" in wf["inputParameters"]
    defaults = wf["inputTemplate"]
    assert defaults.get("repo") == ""
    assert defaults.get("repoPath") == ""
    # Neither is schema-required on its own -- workspace_prepare enforces at
    # least one of repo/repoPath/workspacePath at runtime instead.
    assert "repoPath" not in wf["inputSchema"]["data"]["required"]
    assert "repo" not in wf["inputSchema"]["data"]["required"]
    workspace = next(x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_workspace")
    assert workspace["name"] == "workspace_prepare"
    assert workspace["inputParameters"]["repoPath"] == "${workflow.input.repoPath}"
    assert workspace["inputParameters"]["repoUrl"] == "${workflow.input.repo}"


def test_campaign_design_phase_delegates_to_design_docs():
    # feature_campaign's own design_loop and design_docs.json (already used
    # by issue_to_pr via code_parallel) were two separately-maintained
    # implementations of the same idea. design_docs.json is now the shared
    # superset (human OR agent-judge review, adjustable per-iteration
    # budget/turns, graceful stop) both callers use -- issue_to_pr indirectly
    # via code_parallel, feature_campaign directly here.
    wf = _load("feature_campaign")
    gate = next(x for x in _walk(wf) if x.get("taskReferenceName") == "design_needed_gate")
    assert gate["type"] == "SWITCH"
    # Skips calling design_docs at all when an imported plan already approved
    # the design -- matches the old design_loop's own condition exactly
    # (a DO_WHILE with designApproved already true never runs its body).
    assert "designApproved" in gate["expression"]

    call = next(t for t in gate["decisionCases"]["run"]
               if t["taskReferenceName"] == "campaign_design_docs")
    assert call["type"] == "SUB_WORKFLOW"
    assert call["subWorkflowParam"] == {"name": "design_docs", "version": 1}
    assert call["inputParameters"]["repoPath"] == "${campaign_workspace.output.worktreePath}"
    assert call["inputParameters"]["instruction"] == "${workflow.variables.effectiveInstruction}"
    assert call["inputParameters"]["humanApproval"] == "${workflow.input.designHumanApproval}"
    # feature_campaign never hard-fails the whole campaign just because a
    # design wasn't approved -- it degrades the campaign outcome instead.
    assert call["inputParameters"]["failClosed"] is False
    # The campaign's own adjustable budget pool seeds design_docs' starting
    # point, matching what design_loop used to pass directly.
    assert call["inputParameters"]["designMaxTurns"] == "${workflow.variables.campaignMaxTurns}"
    assert call["inputParameters"]["designMaxBudgetUsd"] == "${workflow.variables.campaignMaxBudgetUsd}"

    capture = next(t for t in gate["decisionCases"]["run"]
                  if t["taskReferenceName"] == "capture_design_result")
    assert capture["inputParameters"]["designApproved"] == "${campaign_design_docs.output.approved}"
    # A human-adjusted budget/turns during design must carry forward into
    # the shared campaign-wide pool the later plan/implementation/final
    # phases also draw from and adjust -- otherwise an adjustment made
    # during design would be silently lost once the sub-workflow returns.
    assert capture["inputParameters"]["campaignMaxTurns"] == "${campaign_design_docs.output.finalMaxTurns}"
    assert capture["inputParameters"]["campaignMaxBudgetUsd"] == "${campaign_design_docs.output.finalMaxBudgetUsd}"

    stopped_gate = next(t for t in gate["decisionCases"]["run"]
                        if t["taskReferenceName"] == "design_stopped_gate")
    assert stopped_gate["inputParameters"]["stopped"] == "${campaign_design_docs.output.stopped}"
    stop_outcome = stopped_gate["decisionCases"]["true"][0]
    assert stop_outcome["inputParameters"]["outcome"] == "incomplete"

    # No dangling reference to the removed inline design_loop/campaign_design
    # task refs anywhere else in the file (usage accounting, output prompt
    # template, etc.) -- this is exactly the class of bug a partial rewire leaves.
    rendered = json.dumps(wf)
    assert '"campaign_design.' not in rendered
    assert "${design_loop." not in rendered
    # designPromptTemplate must NOT read campaign_design_docs.output directly
    # in outputParameters: that task lives inside design_needed_gate's "run"
    # SWITCH case, so reading it unconditionally would be null whenever the
    # "skip" branch is taken (e.g. an imported, already-approved plan) --
    # audit_harness_invariants.py's CONDITIONAL_OUTPUT_REFERENCE check exists
    # precisely to catch this. It must be captured into a workflow variable
    # by capture_design_result first, then read from there -- the only place
    # ${campaign_design_docs.output.promptTemplate} appears is that one capture.
    assert rendered.count("${campaign_design_docs.output.promptTemplate}") == 1
    assert capture["inputParameters"]["designPromptTemplateResult"] == \
        "${campaign_design_docs.output.promptTemplate}"
    assert wf["outputParameters"]["designPromptTemplate"] == \
        "${workflow.variables.designPromptTemplateResult}"


def test_campaign_plan_phase_delegates_to_dag_plan_approval():
    # plan_loop had no existing analog to reconcile with (unlike design_loop):
    # code_parallel's own "plan_loop" is a fully-automatic validator-repair
    # loop with no human involved, not a from-scratch author+human-review
    # loop -- so this is a standalone extraction, faithfully preserving
    # plan_loop's existing behavior (it already had adjustable budget/turns
    # and continue/revise/adopt_edits/stop, unlike design_loop before its
    # own reconciliation).
    wf = _load("feature_campaign")
    gate = next(x for x in _walk(wf) if x.get("taskReferenceName") == "plan_needed_gate")
    assert gate["type"] == "SWITCH"
    # Skips calling dag_plan_approval at all when an imported plan already
    # approved the plan -- matches the old plan_loop's own condition exactly.
    assert "planApproved" in gate["expression"]

    call = next(t for t in gate["decisionCases"]["run"]
               if t["taskReferenceName"] == "campaign_plan_approval")
    assert call["type"] == "SUB_WORKFLOW"
    assert call["subWorkflowParam"] == {"name": "dag_plan_approval", "version": 1}
    assert call["inputParameters"]["callerWorkflow"] == "feature_campaign"
    assert call["inputParameters"]["repoPath"] == "${campaign_workspace.output.worktreePath}"
    assert call["inputParameters"]["instruction"] == "${workflow.variables.effectiveInstruction}"
    # The campaign's own adjustable budget pool seeds dag_plan_approval's
    # starting point, matching what plan_loop used to read directly.
    assert call["inputParameters"]["planMaxTurns"] == "${workflow.variables.campaignMaxTurns}"
    assert call["inputParameters"]["planMaxBudgetUsd"] == "${workflow.variables.campaignMaxBudgetUsd}"

    capture = next(t for t in gate["decisionCases"]["run"]
                  if t["taskReferenceName"] == "capture_plan_result")
    assert capture["inputParameters"]["planApproved"] == "${campaign_plan_approval.output.approved}"
    assert capture["inputParameters"]["remainingTaskIds"] == \
        "${campaign_plan_approval.output.remainingTaskIds}"
    # allowedWriteRoots restricts implementation subtasks to the approved
    # plan's own declared file scope -- it used to be reachable directly from
    # the plan_loop DO_WHILE's own inner task ref (a Conductor loop's inner
    # task refs stay queryable after the loop ends); once extracted into an
    # isolated sub-workflow execution, that's no longer reachable at all
    # unless explicitly surfaced through the sub-workflow's own output and
    # captured into a workflow variable here.
    assert capture["inputParameters"]["allowedWriteRoots"] == \
        "${campaign_plan_approval.output.allowedWriteRoots}"
    # Carries the (possibly human-adjusted) budget forward into the shared
    # campaign-wide pool implementation/final also draw from.
    assert capture["inputParameters"]["campaignMaxTurns"] == \
        "${campaign_plan_approval.output.finalMaxTurns}"
    assert capture["inputParameters"]["campaignMaxBudgetUsd"] == \
        "${campaign_plan_approval.output.finalMaxBudgetUsd}"

    stopped_gate = next(t for t in gate["decisionCases"]["run"]
                        if t["taskReferenceName"] == "plan_stopped_gate")
    assert stopped_gate["inputParameters"]["stopped"] == "${campaign_plan_approval.output.stopped}"
    stop_outcome = stopped_gate["decisionCases"]["true"][0]
    assert stop_outcome["inputParameters"]["outcome"] == "incomplete"

    # Every use of the plan's declared file scope downstream (implementation
    # subtask calls) must read the captured variable, not the now-unreachable
    # nested task ref.
    rendered = json.dumps(wf)
    assert '"campaign_plan.' not in rendered
    assert "${validate_campaign_plan." not in rendered
    assert "${plan_loop." not in rendered
    # Two of the original three uses (adopt_edits/revise wave sessions) now
    # live inside implementation_waves.json, reading it as their own
    # workflow.input.allowedWriteRoots instead -- see
    # test_campaign_implementation_phase_delegates_to_implementation_waves.
    # The two remaining here are the campaign_plan_approval call itself and
    # the campaign_final_verification call (final_verifier's own read now
    # lives inside final_verification.json).
    assert rendered.count("${workflow.variables.allowedWriteRoots}") >= 2

    # planPromptTemplate has the same CONDITIONAL_OUTPUT_REFERENCE hazard
    # designPromptTemplate does (see
    # test_campaign_design_phase_delegates_to_design_docs): campaign_plan_approval
    # only runs inside plan_needed_gate's "run" case, so outputParameters must
    # read a workflow variable capture_plan_result populated, not the task
    # output directly.
    assert rendered.count("${campaign_plan_approval.output.promptTemplate}") == 1
    assert capture["inputParameters"]["planPromptTemplateResult"] == \
        "${campaign_plan_approval.output.promptTemplate}"
    assert wf["outputParameters"]["planPromptTemplate"] == \
        "${workflow.variables.planPromptTemplateResult}"


def test_campaign_implementation_phase_delegates_to_implementation_waves():
    # code_parallel's own parallel-implementation mechanism is a single
    # non-staged fan-out/fan-in with no per-wave human checkpoint -- a
    # different concept from feature_campaign's multi-wave, checkpoint-per-wave
    # engine -- so this is a standalone extraction like dag_plan_approval,
    # not a merge.
    wf = _load("feature_campaign")
    gate = next(x for x in _walk(wf) if x.get("taskReferenceName") == "implementation_needed_gate")
    assert gate["type"] == "SWITCH"
    # Skips calling implementation_waves at all once nothing remains to
    # implement, or an earlier phase already stopped the campaign -- matches
    # the old implementation_loop's own condition exactly.
    assert "outcome" in gate["expression"] and "remaining" in gate["expression"]

    call = next(t for t in gate["decisionCases"]["run"]
               if t["taskReferenceName"] == "campaign_implementation_waves")
    assert call["type"] == "SUB_WORKFLOW"
    assert call["subWorkflowParam"] == {"name": "implementation_waves", "version": 1}
    assert call["inputParameters"]["callerWorkflow"] == "feature_campaign"
    assert call["inputParameters"]["repoPath"] == "${campaign_workspace.output.worktreePath}"
    assert call["inputParameters"]["plan"] == "${workflow.variables.plan}"
    assert call["inputParameters"]["remainingTaskIds"] == "${workflow.variables.remainingTaskIds}"
    assert call["inputParameters"]["allowedWriteRoots"] == "${workflow.variables.allowedWriteRoots}"
    # The campaign's own adjustable budget pool seeds implementation_waves'
    # starting point, matching what implementation_loop used to read directly.
    assert call["inputParameters"]["implementationMaxTurns"] == "${workflow.variables.campaignMaxTurns}"
    assert call["inputParameters"]["implementationMaxBudgetUsd"] == \
        "${workflow.variables.campaignMaxBudgetUsd}"

    capture = next(t for t in gate["decisionCases"]["run"]
                  if t["taskReferenceName"] == "capture_implementation_result")
    assert capture["inputParameters"]["outcome"] == "${campaign_implementation_waves.output.outcome}"
    assert capture["inputParameters"]["completedTaskIds"] == \
        "${campaign_implementation_waves.output.completedTaskIds}"
    assert capture["inputParameters"]["remainingTaskIds"] == \
        "${campaign_implementation_waves.output.remainingTaskIds}"
    # Carries the (possibly human-adjusted) budget forward into the shared
    # campaign-wide pool the final phase also draws from.
    assert capture["inputParameters"]["campaignMaxTurns"] == \
        "${campaign_implementation_waves.output.finalMaxTurns}"
    assert capture["inputParameters"]["campaignMaxBudgetUsd"] == \
        "${campaign_implementation_waves.output.finalMaxBudgetUsd}"

    # No dangling reference to the removed inline implementation_loop's inner
    # task refs anywhere else in the file (usage accounting, campaign_summary
    # session merging, etc.).
    rendered = json.dumps(wf)
    assert "${wave_decision." not in rendered
    assert "${wave_review." not in rendered
    assert "${integrate_wave." not in rendered
    assert "${schedule_wave." not in rendered
    assert "${merge_campaign_state." not in rendered

    # Same callerWorkflow correctness property as design_docs/dag_plan_approval:
    # wave_checkpoint must report the real top-level caller, not this
    # sub-workflow's own name.
    implementation_waves = _load("implementation_waves")
    wave_gate = next(x for x in _walk(implementation_waves) if x.get("taskReferenceName") == "wave_checkpoint")
    assert wave_gate["inputParameters"]["workflow"] == "${workflow.input.callerWorkflow}"
    assert "callerWorkflow" in implementation_waves["inputSchema"]["data"]["required"]
    assert "${implementation_loop." not in rendered


def test_campaign_final_phase_delegates_to_final_verification():
    # Neither code_parallel nor issue_to_pr has a human-reviewed final-
    # verification checkpoint (issue_to_pr's own delivery_audit is a
    # deterministic, non-interactive summary), so this is a standalone
    # extraction like dag_plan_approval/implementation_waves, not a merge.
    wf = _load("feature_campaign")
    gate = next(x for x in _walk(wf) if x.get("taskReferenceName") == "final_needed_gate")
    assert gate["type"] == "SWITCH"
    # Unlike design/plan (which skip once already approved via an imported
    # plan), final verification has no "already done" precondition -- it
    # only skips once an earlier phase has already taken the campaign out of
    # "running" (e.g. an explicit stop), so a human is never asked to review
    # a final state that's already been abandoned. The original final_loop's
    # DO_WHILE ran its body at least once regardless of this, a latent gap
    # this gate closes to match implementation_needed_gate's own precedent.
    assert "outcome" in gate["expression"]

    call = next(t for t in gate["decisionCases"]["run"]
               if t["taskReferenceName"] == "campaign_final_verification")
    assert call["type"] == "SUB_WORKFLOW"
    assert call["subWorkflowParam"] == {"name": "final_verification", "version": 1}
    assert call["inputParameters"]["callerWorkflow"] == "feature_campaign"
    assert call["inputParameters"]["repoPath"] == "${campaign_workspace.output.worktreePath}"
    assert call["inputParameters"]["instruction"] == "${workflow.variables.effectiveInstruction}"
    assert call["inputParameters"]["allowedWriteRoots"] == "${workflow.variables.allowedWriteRoots}"
    assert call["inputParameters"]["finalMaxIterations"] == "${workflow.input.finalMaxRevisions}"
    # The campaign's own adjustable budget pool seeds final_verification's
    # starting point, matching what final_loop used to read directly.
    assert call["inputParameters"]["finalMaxTurns"] == "${workflow.variables.campaignMaxTurns}"
    assert call["inputParameters"]["finalMaxBudgetUsd"] == "${workflow.variables.campaignMaxBudgetUsd}"

    capture = next(t for t in gate["decisionCases"]["run"]
                  if t["taskReferenceName"] == "capture_final_result")
    # outcome is captured directly (verified/incomplete/still-running), not
    # via a separate stopped flag -- final_limit_gate's existing SWITCH on
    # workflow.variables.outcome already distinguishes all three cases
    # exactly like it did when final_loop set outcome inline.
    assert capture["inputParameters"]["outcome"] == "${campaign_final_verification.output.outcome}"
    assert capture["inputParameters"]["feedback"] == "${campaign_final_verification.output.feedback}"
    assert capture["inputParameters"]["lastReviewFindings"] == \
        "${campaign_final_verification.output.reviewFindings}"
    assert capture["inputParameters"]["campaignMaxTurns"] == \
        "${campaign_final_verification.output.finalMaxTurns}"
    assert capture["inputParameters"]["campaignMaxBudgetUsd"] == \
        "${campaign_final_verification.output.finalMaxBudgetUsd}"

    # reviewPromptTemplate has the same CONDITIONAL_OUTPUT_REFERENCE hazard
    # designPromptTemplate/planPromptTemplate do (see
    # test_campaign_design_phase_delegates_to_design_docs): campaign_final_verification
    # only runs inside final_needed_gate's "run" case, so outputParameters
    # must read a workflow variable capture_final_result populated, not the
    # task output directly.
    assert capture["inputParameters"]["finalPromptTemplateResult"] == \
        "${campaign_final_verification.output.promptTemplate}"
    assert wf["outputParameters"]["reviewPromptTemplate"] == \
        "${workflow.variables.finalPromptTemplateResult}"

    # final_limit_gate is untouched -- it already SWITCHes directly on
    # workflow.variables.outcome, which capture_final_result populates the
    # same way the inline final_loop always did.
    limit_gate = next(x for x in _walk(wf) if x.get("taskReferenceName") == "final_limit_gate")
    assert limit_gate["inputParameters"]["outcome"] == "${workflow.variables.outcome}"
    assert set(limit_gate["decisionCases"]) == {"incomplete", "verified"}

    # No dangling reference to the removed inline final_loop's inner task
    # refs anywhere else in the file (usage accounting, output params, etc.).
    rendered = json.dumps(wf)
    assert "${final_loop." not in rendered
    assert "${final_verifier." not in rendered
    assert "${final_decision." not in rendered
    assert "${final_checkpoint." not in rendered
    assert "${final_action." not in rendered
    assert rendered.count("${campaign_final_verification.output.promptTemplate}") == 1

    # Same callerWorkflow correctness property as
    # design_docs/dag_plan_approval/implementation_waves: final_checkpoint
    # must report the real top-level caller, not this sub-workflow's own name.
    final_verification = _load("final_verification")
    final_gate = next(x for x in _walk(final_verification) if x.get("taskReferenceName") == "final_checkpoint")
    assert final_gate["inputParameters"]["workflow"] == "${workflow.input.callerWorkflow}"
    assert "callerWorkflow" in final_verification["inputSchema"]["data"]["required"]


def test_design_docs_supports_agent_judge_review_and_a_graceful_stop():
    # The reconciliation feature_campaign needed: human OR agent-judge review
    # (already existed), adjustable per-iteration budget/turns (new), and no
    # hard TERMINATE when a design is never approved (new, opt-in via
    # failClosed:false so issue_to_pr/code_parallel's existing hard-fail
    # behavior is untouched by default).
    wf = _load("design_docs")
    assert wf["inputTemplate"]["failClosed"] is True
    assert wf["inputTemplate"]["humanApproval"] is True

    review_gate = next(x for x in _walk(wf) if x.get("taskReferenceName") == "design_review")
    assert review_gate["inputParameters"]["availableActions"] == \
        ["continue", "revise", "adopt_edits", "stop"]

    # Both existing TUI clients (chat's _decide_approval, the ApprovalModal
    # widget) send the legacy {"approved": bool, "feedback": str} shape for
    # design_docs, with no "action" field (or chat's own "approve" literal,
    # which isn't a valid checkpoint action either) -- this normalizer must
    # keep both working without any client-side changes required.
    normalize = next(x for x in _walk(wf) if x.get("taskReferenceName") == "design_review_normalize")
    assert "known.indexOf(action) === -1" in normalize["inputParameters"]["expression"]
    assert "d.approved" in normalize["inputParameters"]["expression"]

    checkpoint = next(x for x in _walk(wf) if x.get("taskReferenceName") == "design_review_checkpoint")
    assert checkpoint["name"] == "campaign_checkpoint"
    assert checkpoint["inputParameters"]["decision"] == "${design_review_normalize.output.result}"
    assert checkpoint["inputParameters"]["blockingStatus"] == "${design.output.status}"

    limits = next(x for x in _walk(wf) if x.get("taskReferenceName") == "design_limits")
    assert limits["inputParameters"]["currentMaxTurns"] == "${design_review_checkpoint.output.maxTurns}"
    design_task = next(x for x in _walk(wf) if x.get("taskReferenceName") == "design")
    assert design_task["inputParameters"]["maxTurns"] == "${workflow.variables.currentMaxTurns}"

    stop_case = next(x for x in _walk(wf) if x.get("taskReferenceName") == "design_stopped")
    assert stop_case["inputParameters"]["designStopped"] is True

    loop = next(x for x in _walk(wf) if x.get("taskReferenceName") == "design_loop")
    assert "!$.stopped" in loop["loopCondition"]

    fail_gate = next(x for x in _walk(wf) if x.get("taskReferenceName") == "fail_closed_gate")
    assert fail_gate["inputParameters"]["failClosed"] == "${workflow.input.failClosed}"
    assert fail_gate["decisionCases"]["true"][0]["type"] == "TERMINATE"
    assert fail_gate["defaultCase"] == []


def test_campaign_pauses_at_each_phase_and_uses_dynamic_subworkflow():
    wf = _load("feature_campaign")
    gates = [x for x in _walk(wf) if x.get("type") == "WAIT"]
    # design's own WAIT checkpoint (design_review) lives inside the shared
    # design_docs sub-workflow, plan's (plan_checkpoint) inside
    # dag_plan_approval, wave's (wave_checkpoint) inside implementation_waves,
    # final's (final_checkpoint) inside final_verification -- see
    # test_campaign_design_phase_delegates_to_design_docs,
    # test_campaign_plan_phase_delegates_to_dag_plan_approval,
    # test_campaign_implementation_phase_delegates_to_implementation_waves,
    # and test_campaign_final_phase_delegates_to_final_verification for those
    # gates. No WAIT checkpoint remains directly in feature_campaign itself.
    assert gates == []
    design_docs = _load("design_docs")
    design_gates = [x for x in _walk(design_docs) if x.get("type") == "WAIT"]
    assert {g["inputParameters"]["phase"] for g in design_gates} == {"design"}
    dag_plan_approval = _load("dag_plan_approval")
    plan_gates = [x for x in _walk(dag_plan_approval) if x.get("type") == "WAIT"]
    assert {g["inputParameters"]["phase"] for g in plan_gates} == {"plan"}
    implementation_waves = _load("implementation_waves")
    wave_gates = [x for x in _walk(implementation_waves) if x.get("type") == "WAIT"]
    assert {g["inputParameters"]["phase"] for g in wave_gates} == {"wave"}
    final_verification = _load("final_verification")
    final_gates = [x for x in _walk(final_verification) if x.get("type") == "WAIT"]
    assert {g["inputParameters"]["phase"] for g in final_gates} == {"final"}
    fork = next(x for x in _walk(implementation_waves) if x.get("type") == "FORK_JOIN_DYNAMIC")
    assert fork["dynamicForkTasksParam"] == "dynamicTasks"
    assert "campaign_subtask" in (WF / "campaign_subtask.json").read_text()


def test_campaign_state_merge_uses_the_typed_worker_not_jq():
    # merge_campaign_state now lives inside implementation_waves.json.
    wf = _load("implementation_waves")
    state_merge = next(x for x in _walk(wf) if x.get("taskReferenceName") == "merge_campaign_state")
    assert state_merge["type"] == "SIMPLE"
    assert state_merge["name"] == "campaign_merge_state"
    assert "queryExpression" not in state_merge["inputParameters"]


def test_campaign_commits_locally_and_only_publishes_when_requested():
    wf = _load("feature_campaign")
    assert wf["inputTemplate"]["createPr"] is False
    assert wf["inputTemplate"]["prBase"] == "main"
    commit = next(x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_commit")
    assert commit["name"] == "commit"
    publication_plan = next(
        x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_publication_plan"
    )
    assert publication_plan["inputParameters"]["outcome"] == "${workflow.variables.outcome}"
    assert publication_plan["name"] == "campaign_publication_plan"
    from common import feature_campaign as feature_campaign_logic
    assert "draft" in feature_campaign_logic.resolve_campaign_publication_plan(
        outcome="incomplete", requested_draft=False, test_state=None,
        tests_passed=None, tested=None, committed="c")
    publish_gate = next(x for x in _walk(wf) if x.get("taskReferenceName") == "campaign_publish_gate")
    assert publish_gate["inputParameters"]["createPr"] == "${workflow.input.createPr}"
    publication = publish_gate["decisionCases"]["true"]
    assert [task["name"] for task in publication] == ["git_push", "campaign_push_result_gate"]
    # The post-commit test cycle can advance the candidate, so the exact-SHA
    # push guard reads a resolved publishCandidateCommit variable rather than
    # the pre-test commit, which would now present as drift and block the
    # push. When requirePrApproval is false (the default), skip_campaign_pr_approval
    # sets that variable straight from the publication plan's head, so the
    # guarantee is unchanged; when true, pr_draft_approval's own (possibly
    # revised) candidateCommit flows through instead.
    assert publication[0]["inputParameters"]["expectedHead"] == \
        "${workflow.variables.publishCandidateCommit}"
    skip = next(x for x in _walk(wf) if x.get("taskReferenceName") == "skip_campaign_pr_approval")
    assert skip["inputParameters"]["publishCandidateCommit"] == \
        "${campaign_publication_plan.output.head}"
    push_gate = publication[1]
    outcome_gate = push_gate["decisionCases"]["true"][0]
    assert outcome_gate["taskReferenceName"] == "campaign_pr_approval_outcome_gate"
    assert [task["name"] for task in outcome_gate["decisionCases"]["approved"]] == \
        ["pr_create", "campaign_capture_publication"]
    assert wf["outputParameters"]["prUrl"] == "${workflow.variables.prUrl}"
    handoff = wf["outputParameters"]["sourceHandoff"]
    assert handoff["repoPath"] == "${campaign_summary.output.repoPath}"
    assert handoff["commit"] == "${campaign_summary.output.candidateCommit}"
    assert handoff["branch"] == "${campaign_summary.output.branch}"
    assert handoff["originalBranch"] == "${campaign_workspace.output.originalBranch}"
    assert handoff["includedSourcePaths"] == "${campaign_workspace.output.baselineIncludedPaths}"
    assert handoff["presented"] is True


def test_every_new_simple_has_a_registered_definition_without_deadlines():
    names = {x["name"] for wf_name in ("feature_campaign", "campaign_subtask")
             for x in _walk(_load(wf_name)) if x.get("type") == "SIMPLE"}
    defs = {json.loads(p.read_text())["name"]: json.loads(p.read_text())
            for p in (WF / "taskdefs").glob("*.json")}
    assert not (names - set(defs))
    for name in names:
        if name.startswith("campaign_"):
            assert defs[name]["pollTimeoutSeconds"] == 0
            assert defs[name]["responseTimeoutSeconds"] == 0
            assert defs[name]["timeoutSeconds"] == 0


def test_code_parallel_remains_independent_from_campaign():
    wf = _load("code_parallel")
    assert wf["name"] == "code_parallel" and wf["version"] == 1
    assert "feature_campaign" not in json.dumps(wf)
