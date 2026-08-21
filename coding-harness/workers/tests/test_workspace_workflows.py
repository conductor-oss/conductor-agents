from __future__ import annotations

import json
from pathlib import Path


WF = Path(__file__).resolve().parents[1] / "workflows"
LOCAL_BRANCH_OWNERS = {"code_parallel", "feature_campaign", "openspec_development"}
REMOTE_BRANCH_OWNERS = {"github_demo", "issue_to_pr", "publish_salvage"}
EXISTING_PR_BRANCH_UPDATERS = {"address_pr", "address_pr_repair", "address_pr_approval", "publish_verified_pr"}
READ_ONLY = {"local_review", "pr_review", "document_plan", "openspec_plan", "runtime_health"}
ORCHESTRATORS = {
    "automation_dispatch", "automation_reset", "issue_resolution_sweep",
    "pr_address_sweep", "pr_review_sweep",
}
NESTED_BRANCH_CONSUMERS = {
    "campaign_subtask", "code_revision_loop", "code_subtask", "design_docs",
    "merge_remediation", "openspec_artifact_drain", "openspec_generate_artifact",
    # test_cycle owns no branch: it commits its fixes to whatever branch the
    # caller's repoPath is already on, exactly like the loop it replaces.
    # test_agent_fallback is test_cycle's own extracted sub-workflow (the
    # agent-assisted discovery/authoring fallback) and inherits the same
    # property: it commits an authored test to test_cycle's own branch, never
    # creates or pushes one of its own.
    "test_cycle", "test_agent_fallback",
    # pr_draft_approval owns no branch either: a requested revision's
    # code_parallel call is pinned to the caller's already-existing candidate
    # branch (changeBranch), the same pre-publish local branch issue_to_pr
    # (or feature_campaign) already produced -- there is no PR yet to update,
    # unlike address_pr_approval's already-published EXISTING_PR_BRANCH_UPDATERS case.
    "pr_draft_approval",
    # dag_plan_approval owns no branch either: it only authors a plan
    # document and commits nothing itself -- the caller's own workspace
    # (already on its own branch) is used purely as a read/write scratch
    # area for the plan-authoring agent.
    "dag_plan_approval",
    # implementation_waves owns no branch either: every wave/revision/adopt
    # commit lands on the caller's already-existing candidate branch
    # (repoPath), exactly like the loop it replaces.
    "implementation_waves",
    # final_verification owns no branch either: the verified/adopted commit
    # lands on the caller's already-existing candidate branch (repoPath),
    # exactly like the final_loop it replaces.
    "final_verification",
}


def _load(name: str) -> dict:
    return json.loads((WF / f"{name}.json").read_text())


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_every_workflow_has_an_explicit_branch_ownership_classification():
    classified = set().union(
        LOCAL_BRANCH_OWNERS, REMOTE_BRANCH_OWNERS, EXISTING_PR_BRANCH_UPDATERS,
        READ_ONLY, ORCHESTRATORS, NESTED_BRANCH_CONSUMERS,
    )
    actual = {path.stem for path in WF.glob("*.json")}
    assert classified == actual


def test_all_local_branch_owners_publish_the_actual_workspace_handoff():
    for name in LOCAL_BRANCH_OWNERS:
        workflow = _load(name)
        assert workflow["version"] == 1
        assert workflow["inputTemplate"]["workspacePath"] == ""
        serialized = json.dumps(workflow)
        assert '"name": "workspace_prepare"' in serialized
        assert '"name": "prepare_repo"' not in serialized
        assert '"name": "create_branch"' not in serialized
        handoff = workflow["outputParameters"]["sourceHandoff"]
        assert isinstance(handoff, dict)
        assert handoff["branch"].endswith(".output.branch}")
        assert "workflow.input.changeBranch" not in handoff["branch"]
        assert {"sourceRepoPath", "repoPath", "branch", "commit",
                "originalBranch", "originalHead", "baselineCommit",
                "includedSourcePaths", "presented"} <= set(handoff)


def test_every_new_publication_branch_is_bound_to_a_run_identity():
    for name in LOCAL_BRANCH_OWNERS:
        workflow = _load(name)
        workspace = next(node for node in _walk(workflow)
                         if node.get("name") == "workspace_prepare")
        assert "branchRunId" in workspace["inputParameters"], name

    code_parallel = _load("code_parallel")
    assert code_parallel["inputTemplate"]["branchRunId"] == ""
    code_workspace = next(node for node in _walk(code_parallel)
                          if node.get("name") == "workspace_prepare")
    assert code_workspace["inputParameters"]["branchRunId"] == \
        "${workflow.input.branchRunId}"

    issue = _load("issue_to_pr")
    issue_builders = [node for node in _walk(issue)
                      if node.get("type") == "SUB_WORKFLOW"
                      and node.get("subWorkflowParam", {}).get("name") == "code_parallel"]
    # Only the initial implementation call remains directly in issue_to_pr.json;
    # the revision call now lives in pr_draft_approval.json (extracted from
    # issue_to_pr's own pr_approval_loop so feature_campaign can reuse it too).
    assert len(issue_builders) == 1
    assert issue_builders[0]["inputParameters"]["branchRunId"] == "${workflow.workflowId}"

    draft_approval = _load("pr_draft_approval")
    revise_builders = [node for node in _walk(draft_approval)
                       if node.get("type") == "SUB_WORKFLOW"
                       and node.get("subWorkflowParam", {}).get("name") == "code_parallel"]
    assert len(revise_builders) == 1
    # No branchRunId: changeBranch is always the caller's already-existing
    # candidate branch here (never blank), so there's no fresh name to
    # disambiguate -- matches address_pr_approval's own revise call.
    assert "branchRunId" not in revise_builders[0]["inputParameters"]
    assert revise_builders[0]["inputParameters"]["changeBranch"] == \
        "${workflow.variables.currentBranch}"

    demo_branch = next(node for node in _walk(_load("github_demo"))
                       if node.get("name") == "create_branch")
    assert demo_branch["inputParameters"]["branchRunId"] == "${workflow.workflowId}"

    openspec_finalize = next(node for node in _walk(_load("openspec_development"))
                             if node.get("name") == "openspec_finalize")
    assert openspec_finalize["inputParameters"]["branchRunId"] == \
        "${workflow.workflowId}"


def test_existing_branch_workflows_do_not_generate_replacement_branch_names():
    exact_branch_workflows = EXISTING_PR_BRANCH_UPDATERS | {"publish_salvage", "publish_verified_pr"}
    for name in exact_branch_workflows:
        serialized = json.dumps(_load(name))
        assert '"name": "workspace_prepare"' not in serialized, name
        assert '"name": "create_branch"' not in serialized, name


def test_local_branch_is_prepared_before_any_target_mutation():
    mutators = {
        "campaign_design", "campaign_plan", "coding_agent", "openspec_intake",
        "commit", "merge_worktrees", "openspec_finalize",
    }
    for name in LOCAL_BRANCH_OWNERS:
        workflow = _load(name)
        tasks = workflow["tasks"]
        owner_index = next(index for index, task in enumerate(tasks)
                           if task.get("name") == "workspace_prepare")
        for index, task in enumerate(tasks):
            if task.get("name") in mutators:
                assert owner_index < index, f"{name}: {task.get('taskReferenceName')} mutates before branch ownership"


def test_nested_workflows_pin_current_versions_and_inherit_parent_workspace():
    for name in ("issue_to_pr", "address_pr", "openspec_development"):
        workflow = _load(name)
        children = [node for node in _walk(workflow) if node.get("type") == "SUB_WORKFLOW"]
        assert children
        for child in children:
            target = child["subWorkflowParam"]["name"]
            assert child["subWorkflowParam"]["version"] == _load(target)["version"]
        if name == "openspec_development":
            assert "workspacePath" in json.dumps(workflow)

    from common import code_parallel as code_parallel_logic

    code_parallel = _load("code_parallel")
    assert code_parallel["inputTemplate"]["workspacePath"] == ""
    build_forks = next(node for node in _walk(code_parallel)
                       if node.get("taskReferenceName") == "build_forks")
    assert build_forks["name"] == "code_parallel_build_forks"
    # The dynamic-fork pin used to be a literal string in a jq expression that
    # register.sh's stale-version regex could grep for; it is Python now, so
    # the pin is asserted directly against the function that builds it.
    forks = code_parallel_logic.build_forks(
        repo_path="/tmp", subtasks=[{"id": "a", "description": "d", "files": ["a.py"]}],
        change_dir="", code_model="", code_prompt_template="", code_prompt_template_source="",
        spec_context_path="", context_paths=[], max_turns=1, max_budget_usd=1,
        model_profile="", model_policy={}, model_policy_source="", model_policy_sha256="",
        models_config="", model_overrides={})
    assert forks["dynamicTasks"][0]["subWorkflowParam"] == \
        {"name": "code_subtask", "version": _load("code_subtask")["version"]}


def test_code_parallel_returns_source_handoff_after_plan_validation_and_merge():
    workflow = _load("code_parallel")
    refs = [task["taskReferenceName"] for task in workflow["tasks"]]
    assert refs.index("plan_loop") < refs.index("build_forks")
    assert refs.index("merge") < refs.index("verification_candidate_init")
    assert refs.index("verification_candidate_init") < refs.index("verification_outcome")
    assert refs.index("verification_outcome") < refs.index("source_handoff")
    candidate_init = next(task for task in workflow["tasks"]
                          if task["taskReferenceName"] == "verification_candidate_init")
    assert candidate_init["type"] == "SET_VARIABLE"
    assert candidate_init["inputParameters"]["candidateCommit"] == \
        "${select_merge_candidate.output.commit}"
    handoff = next(task for task in workflow["tasks"]
                   if task["taskReferenceName"] == "source_handoff")
    assert handoff["name"] == "inplace_guard"
    assert handoff["inputParameters"]["expectedHead"] == "${workflow.variables.candidateCommit}"
    assert workflow["outputParameters"]["sourceHandoff"] == {
        "sourceRepoPath": "${workflow.input.repoPath}",
        "repoPath": "${source_handoff.output.repoPath}",
        "branch": "${source_handoff.output.branch}",
        "commit": "${source_handoff.output.head}",
        "originalBranch": "${workspace.output.originalBranch}",
        "originalHead": "${workspace.output.originalHead}",
        "baselineCommit": "${workspace.output.baselineCommit}",
        "includedSourcePaths": "${workspace.output.baselineIncludedPaths}",
        "presented": "${handoff_summary.output.presented}",
        "verificationState": "${verification_outcome.output.state}",
        "executionOutcome": "${verification_outcome.output.executionOutcome}",
        "verificationAttempts": "${plan_loop.output.iteration}",
    }


def test_code_parallel_requires_both_a_valid_plan_and_complete_merge():
    from common import code_parallel

    workflow = _load("code_parallel")
    outcome = next(task for task in workflow["tasks"]
                   if task["taskReferenceName"] == "verification_outcome")
    assert outcome["name"] == "code_parallel_verification_outcome"

    base = dict(candidate_commit="c", delivery={"state": "passed"}, issues=[],
               tested=None, test_state=None, tests_passed=None)
    assert code_parallel.resolve_verification_outcome(
        **base, merge_state="merged", plan_valid=True)["passed"] is True
    assert code_parallel.resolve_verification_outcome(
        **base, merge_state="conflicted", plan_valid=True)["passed"] is False
    assert code_parallel.resolve_verification_outcome(
        **base, merge_state="merged", plan_valid=False)["passed"] is False


def test_code_parallel_rechecks_every_repaired_plan_before_fanout():
    workflow = _load("code_parallel")
    plan_loop = next(task for task in workflow["tasks"]
                     if task["taskReferenceName"] == "plan_loop")
    plan_check = next(task for task in plan_loop["loopOver"]
                      if task["taskReferenceName"] == "plan_check")
    action = next(task for task in plan_loop["loopOver"]
                  if task["taskReferenceName"] == "plan_action")
    repair_branch = action["decisionCases"]["needs_replan"]
    capture = next(task for task in repair_branch
                   if task["taskReferenceName"] == "capture_repaired_plan")

    assert plan_check["name"] == "verification_discover"
    assert plan_check["inputParameters"]["subtasks"] == "${workflow.variables.planSubtasks}"
    assert capture["inputParameters"]["planSubtasks"] == \
        "${plan_repair.output.structured.subtasks}"
    assert capture["inputParameters"]["planValid"] is False
    assert "plan_valid !== true" in plan_loop["loopCondition"]


def test_github_flows_require_a_remote_repository_identifier():
    for name in ("pr_review", "issue_to_pr", "address_pr"):
        workflow = _load(name)
        assert "repo" in workflow["inputParameters"]
        assert "repoPath" not in workflow["inputTemplate"]
