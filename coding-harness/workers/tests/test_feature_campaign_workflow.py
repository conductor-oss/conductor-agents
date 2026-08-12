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
    loops = [x for x in _walk(wf) if x.get("type") == "DO_WHILE"]
    assert len(loops) >= 4
    assert all(x.get("evaluatorType") == "graaljs" for x in loops)
    assert all("iteration" in x["loopCondition"] for x in loops)


def test_campaign_pauses_at_each_phase_and_uses_dynamic_subworkflow():
    wf = _load("feature_campaign")
    gates = [x for x in _walk(wf) if x.get("type") == "WAIT"]
    assert {g["inputParameters"]["phase"] for g in gates} == {"design", "plan", "wave", "final"}
    fork = next(x for x in _walk(wf) if x.get("type") == "FORK_JOIN_DYNAMIC")
    assert fork["dynamicForkTasksParam"] == "dynamicTasks"
    assert "campaign_subtask" in (WF / "campaign_subtask.json").read_text()


def test_campaign_state_merge_uses_the_typed_worker_not_jq():
    wf = _load("feature_campaign")
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
    # push guard reads the head the publication plan resolved rather than the
    # pre-test commit, which would now present as drift and block the push.
    assert publication[0]["inputParameters"]["expectedHead"] == \
        "${campaign_publication_plan.output.head}"
    push_gate = publication[1]
    assert [task["name"] for task in push_gate["decisionCases"]["true"]] == \
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
