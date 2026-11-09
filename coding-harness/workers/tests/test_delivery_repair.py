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


def test_delivery_audit_never_requires_an_optional_path(tmp_git_repo, fake_task_input):
    """Every item with a frozen acceptance test was audited incomplete_delivery because the test —
    writable only so a WRONG assertion can be amended — was a planned path the implementer rightly
    never touched: three no-op repair agents per fork, 15 of 16 forks (run a33d7061)."""
    (tmp_git_repo / "src.py").write_text("print('changed')\n")
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["src.py", "tests/acceptance/test_accept_x.py"],
        optionalPaths=["tests/acceptance/test_accept_x.py"], agentReport={})).output_data
    assert out["state"] == "passed" and out["missingPaths"] == []
    assert out["optionalPaths"] == ["tests/acceptance/test_accept_x.py"]
    from common import code_subtask as code_subtask_logic
    result = code_subtask_logic.resolve_delivery_outcome(
        planned_paths=["src.py", "tests/acceptance/test_accept_x.py"],
        optional_paths=["tests/acceptance/test_accept_x.py"],
        commit={"stagedPaths": ["src.py"], "rejectedPaths": [], "commit": "abc", "noOp": False},
        audit={}, agent={}, tested=None, test_state=None, tests_passed=None)
    assert result["passed"] is True and result["missingPaths"] == []
    assert result["optionalPaths"] == ["tests/acceptance/test_accept_x.py"]


def test_code_subtask_hands_its_acceptance_tests_to_every_audit_as_optional():
    workflow = _load("code_subtask")
    assert "acceptanceTests" in workflow["inputParameters"]
    assert workflow["inputTemplate"]["acceptanceTests"] == []
    for ref in ("initial_delivery_audit", "repair_delivery_audit", "delivery_outcome"):
        assert _task(workflow, ref)["inputParameters"]["optionalPaths"] == "${workflow.input.acceptanceTests}"


def test_test_cycle_hands_the_declared_write_scope_to_discovery():
    """test_cycle received allowedWriteRoots but never passed it to test_discover, so a fork's repair
    agent was scoped to the discovered DIRECTORY (store/) and implemented sibling items' files to make
    the item's acceptance test pass; those commits then collided with the owners' forks (run a33d7061)."""
    workflow = _load("test_cycle")
    for ref in ("initial_discover", "discover"):
        assert _task(workflow, ref)["inputParameters"]["allowedWriteRoots"] == "${workflow.input.allowedWriteRoots}"
    assert _task(workflow, "repair")["inputParameters"]["allowedWriteRoots"] == "${initial_discover.output.repairScope}"
    from common import test_plan
    assert test_plan.repair_scope(["store/lists.go", "store/lists_test.go"], ["store"]) == \
        ["store/lists.go", "store/lists_test.go"]


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


def test_delivery_audit_lists_each_file_of_a_new_directory(tmp_git_repo, fake_task_input):
    """The first files of a NEW directory showed up in git status as one collapsed entry ``resp/``, so
    the audit reported every planned file of the item missing and three no-op repair agents ran per
    such fork — resp-errors, resp-writer, store-core, server-dispatch (run ec0fb6dc)."""
    (tmp_git_repo / "resp").mkdir()
    (tmp_git_repo / "resp" / "errors.go").write_text("package resp\n")
    (tmp_git_repo / "resp" / "errors_test.go").write_text("package resp\n")
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["resp/errors.go", "resp/errors_test.go"],
        agentReport={"status": "success", "agentCompleted": True})).output_data
    assert out["state"] == "passed" and out["missingPaths"] == []
    assert out["actualPaths"] == ["resp/errors.go", "resp/errors_test.go"]


def test_delivery_audit_counts_a_planned_path_already_on_the_branch_as_delivered(tmp_git_repo, fake_task_input):
    """A review re-dispatch changes only what the reviewer asked for; the item's other files were
    delivered by its earlier attempt and are on the branch already. store-hashes changed only
    hashes_test.go and ran three no-op repair agents for the hashes.go it had delivered a wave earlier,
    then ended incomplete_delivery (run ec0fb6dc)."""
    from common import git
    (tmp_git_repo / "store").mkdir()
    (tmp_git_repo / "store" / "hashes.go").write_text("package store\n")
    git.git(str(tmp_git_repo), "add", "store/hashes.go")
    git.git(str(tmp_git_repo), "commit", "-q", "-m", "earlier attempt")
    (tmp_git_repo / "store" / "hashes_test.go").write_text("package store\n")
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["store/hashes.go", "store/hashes_test.go"],
        agentReport={"status": "success", "agentCompleted": True})).output_data
    assert out["state"] == "passed" and out["missingPaths"] == []
    assert out["presentPaths"] == ["store/hashes.go"] and out["actualPaths"] == ["store/hashes_test.go"]
    # A planned file that exists nowhere is still missing.
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["store/hashes.go", "store/hashes_test.go", "store/new.go"],
        agentReport={"status": "success", "agentCompleted": True})).output_data
    assert out["state"] == "incomplete_delivery" and out["missingPaths"] == ["store/new.go"]
    from common import code_subtask as code_subtask_logic
    result = code_subtask_logic.resolve_delivery_outcome(
        planned_paths=["store/hashes.go", "store/hashes_test.go"],
        commit={"stagedPaths": ["store/hashes_test.go"], "rejectedPaths": [], "commit": "abc", "noOp": False},
        audit={"presentPaths": ["store/hashes.go"]}, agent={"agentCompleted": True},
        tested=None, test_state=None, tests_passed=None)
    assert result["state"] == "passed" and result["missingPaths"] == []
    assert result["presentPaths"] == ["store/hashes.go"]


def test_delivery_audit_reports_a_pushback_and_the_workflow_runs_no_repair(tmp_git_repo, fake_task_input):
    """An implementer that pushed back on a directive (H16) made its case instead of the change; the Lead
    decides at integration. resp-reader's pushback was followed by a $0.57 repair agent that rewrote
    reader.go anyway, and the Lead then discarded that fork (run ec0fb6dc)."""
    report = {"status": "success", "agentCompleted": True,
              "structured": {"summary": "no change", "pushback": {"directive": "widen to docs", "reason": "out of scope"}}}
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["resp/reader.go"], agentReport=report)).output_data
    assert out["state"] == "pushback" and out["complete"] is False
    workflow = _load("code_subtask")
    gate = _task(workflow, "delivery_repair_gate")
    assert [t["type"] for t in gate["decisionCases"]["pushback"]] == ["SET_VARIABLE"]
    assert all(t["type"] != "DO_WHILE" for t in gate["decisionCases"]["pushback"])
    from common import code_subtask as code_subtask_logic
    result = code_subtask_logic.resolve_delivery_outcome(
        planned_paths=["resp/reader.go"], commit={"stagedPaths": [], "rejectedPaths": [], "noOp": True},
        audit={"state": "pushback", "presentPaths": ["resp/reader.go"]}, agent={"agentCompleted": True},
        tested=None, test_state=None, tests_passed=None)
    assert result["state"] == "pushback" and result["passed"] is False


def test_delivery_audit_treats_a_satisfied_claim_as_no_delivery_to_repair(tmp_git_repo, fake_task_input):
    """cmd-dispatch's implementer found its registry and dispatch already written by a sibling inside
    that sibling's file and changed nothing; the audit ran six repair agents that created nothing, then
    the item blocked as an empty delivery (run 90e5e3aa). A structured claim is no delivery to repair:
    the peer reviewer judges it at integration."""
    report = {"status": "success", "agentCompleted": True,
              "structured": {"summary": "no change",
                             "alreadySatisfied": {"evidence": "internal/command/handlers.go holds Registry and Dispatch"}}}
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["internal/command/command.go"], agentReport=report)).output_data
    assert out["state"] == "satisfied" and out["complete"] is False
    workflow = _load("code_subtask")
    gate = _task(workflow, "delivery_repair_gate")
    assert [t["type"] for t in gate["decisionCases"]["satisfied"]] == ["SET_VARIABLE"]
    from common import code_subtask as code_subtask_logic
    result = code_subtask_logic.resolve_delivery_outcome(
        planned_paths=["internal/command/command.go"], commit={"stagedPaths": [], "rejectedPaths": [], "noOp": True},
        audit={"state": "satisfied"}, agent={"agentCompleted": True}, tested=None, test_state=None, tests_passed=None)
    assert result["state"] == "satisfied" and result["passed"] is False
    # A claim made alongside actual changes is an ordinary delivery, audited on its files.
    (tmp_git_repo / "internal" / "command").mkdir(parents=True)
    (tmp_git_repo / "internal" / "command" / "command.go").write_text("package command\n")
    changed = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["internal/command/command.go"], agentReport=report)).output_data
    assert changed["state"] == "passed"
