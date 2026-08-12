from __future__ import annotations

import json
from pathlib import Path

from gitops.tasks import delivery_audit


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


def _load(name: str) -> dict:
    return json.loads((WORKFLOWS / f"{name}.json").read_text())


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _task(workflow: dict, ref: str) -> dict:
    return next(node for node in _walk(workflow)
                if node.get("taskReferenceName") == ref)


def test_delivery_audit_uses_exact_paths_and_structured_failures(
    tmp_git_repo, fake_task_input
):
    (tmp_git_repo / "src.py").write_text("print('changed')\n")
    report = {
        "status": "success",
        "agentCompleted": True,
        "structured": {"note": "diagnostic only"},
        "denials": ["sandbox denied image encoder"],
        "turns": [{"commands": [
            {"command": "make asset", "exitCode": 2, "stderr": "missing tool"},
        ]}],
    }

    result = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo),
        plannedPaths=["src.py", "assets/result.png"],
        agentReport=report,
    ))

    out = result.output_data
    assert out["state"] == "incomplete_delivery"
    assert out["actualPaths"] == ["src.py"]
    assert out["missingPaths"] == ["assets/result.png"]
    assert out["sandboxDenials"] == ["sandbox denied image encoder"]
    assert out["commandFailures"] == [{
        "command": "make asset", "exitCode": 2, "stderr": "missing tool",
    }]
    assert out["previousReport"] == {"note": "diagnostic only"}


def test_code_subtask_has_initial_pass_plus_three_data_driven_repairs():
    workflow = _load("code_subtask")
    initial = _task(workflow, "initial_delivery_audit")
    loop = _task(workflow, "delivery_repair_loop")
    repair = _task(workflow, "delivery_repair")
    context = _task(workflow, "delivery_repair_context")
    commit = _task(workflow, "cmt")
    outcome = _task(workflow, "delivery_outcome")

    assert initial["inputParameters"]["plannedPaths"] == \
        "${workflow.input.allowedWriteRoots}"
    assert "['iteration'] < 3" in loop["loopCondition"]
    assert "deliveryState !== 'passed'" in loop["loopCondition"]
    assert repair["optional"] is True
    assert repair["inputParameters"]["failSoft"] is True
    prompt = repair["inputParameters"]["prompt"]
    for field in ("missingPathsJson", "rejectedPathsJson", "commandFailuresJson",
                  "sandboxDenialsJson", "previousStructuredReportJson"):
        assert field in prompt
    # Serialization moved out of jq into the json_text worker: the task carried
    # a throwing-expression failure surface and three fixture cases for a row of
    # tojson calls and no logic.
    assert context["name"] == "json_text"
    assert context["type"] == "SIMPLE"
    assert set(context["inputParameters"]) == {
        "missingPaths", "rejectedPaths", "commandFailures", "sandboxDenials",
        "previousStructuredReport",
    }
    assert commit["optional"] is True
    assert outcome["inputParameters"]["commit"] == "${cmt.output}"
    assert outcome["name"] == "code_subtask_delivery_outcome"
    from common import code_subtask as code_subtask_logic
    result = code_subtask_logic.resolve_delivery_outcome(
        planned_paths=["a.py", "b.py"], commit={"stagedPaths": ["a.py"], "rejectedPaths": []},
        audit={}, agent={}, tested=None, test_state=None, tests_passed=None)
    assert result["missingPaths"] == ["b.py"]


def test_code_parallel_fail_soft_states_and_partial_merge_routing():
    from common import code_parallel

    workflow = _load("code_parallel")
    build = _task(workflow, "build_forks")
    # build_forks itself is optional:true on the dynamic tasks it emits, not on
    # the (now-worker) task that emits them.
    assert build["name"] == "code_parallel_build_forks"
    forks = code_parallel.build_forks(
        repo_path="/tmp", subtasks=[{"id": "a", "description": "d", "files": ["a.py"]}],
        change_dir="", code_model="", code_prompt_template="", code_prompt_template_source="",
        spec_context_path="", context_paths=[], max_turns=1, max_budget_usd=1,
        model_profile="", model_policy={}, model_policy_source="", model_policy_sha256="",
        models_config={}, model_overrides={})
    assert forks["dynamicTasks"][0]["optional"] is True
    assert _task(workflow, "fan_out")["optional"] is True
    assert _task(workflow, "fan_join")["optional"] is True
    assert _task(workflow, "merge")["optional"] is True

    for state, delivery_state, merge_state, plan_valid in (
        ("passed", "passed", "merged", True),
        ("incomplete_delivery", "incomplete_delivery", "merged", True),
        ("merge_blocked", "passed", "merge_blocked", True),
        ("audit_unavailable", "audit_unavailable", "merged", True),
        ("implementation_unavailable", "implementation_unavailable", "merged", True),
    ):
        outcome = code_parallel.resolve_verification_outcome(
            candidate_commit="c", delivery={"state": delivery_state}, issues=[],
            merge_state=merge_state, plan_valid=plan_valid, tested=None,
            test_state=None, tests_passed=None)
        assert outcome["state"] == state, state


def test_transient_publication_workers_use_three_exponential_retries():
    for name in ("issue_fetch", "git_clone", "workspace_prepare", "git_push", "pr_create"):
        definition = json.loads((WORKFLOWS / "taskdefs" / f"{name}.json").read_text())
        assert definition["retryCount"] == 3
        assert definition["retryLogic"] == "EXPONENTIAL_BACKOFF"
