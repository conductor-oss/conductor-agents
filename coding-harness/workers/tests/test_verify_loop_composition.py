"""Composition tests for code_parallel's plan-only validation/replan loop.

The loop validates the parallel execution contract before fan-out.  It never
selects or runs tests and never validates OpenSpec artifacts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WF = Path(__file__).resolve().parents[1] / "workflows"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq binary not installed")


def _load(name: str) -> dict:
    return json.loads((WF / f"{name}.json").read_text())


def _search(nodes, ref):
    for node in nodes:
        if node.get("taskReferenceName") == ref:
            return node
        if node.get("type") == "DO_WHILE":
            found = _search(node.get("loopOver") or [], ref)
            if found is not None:
                return found
        if node.get("type") == "SWITCH":
            branches = list((node.get("decisionCases") or {}).values())
            branches.append(node.get("defaultCase") or [])
            for branch in branches:
                found = _search(branch, ref)
                if found is not None:
                    return found
    return None


def _find(workflow: dict, ref: str) -> dict:
    found = _search(workflow["tasks"], ref)
    if found is None:
        raise AssertionError(f"task {ref!r} not found")
    return found


def _eval_jq(query: str, data: dict, tmp_path: Path) -> dict:
    program = tmp_path / "program.jq"
    program.write_text(query, encoding="utf-8")
    proc = subprocess.run(["jq", "-f", str(program)], input=json.dumps(data),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_code_parallel_has_one_plan_loop_before_fanout_and_never_inlines_tests():
    """code_parallel no longer avoids testing -- it avoids *inlining* tests.

    The original invariant was that this workflow contained no test discovery or
    execution at all, which meant a parallel change could merge cleanly and
    still be handed forward red.  It now tests after the merge, but only by
    delegating to one versioned ``test_cycle`` sub-workflow.  Inlining
    ``test_discover``/``test_run`` here would recreate the per-workflow
    test-command sprawl the original invariant existed to prevent, so that half
    of it is still enforced below.
    """
    workflow = _load("code_parallel")
    refs = [task["taskReferenceName"] for task in workflow["tasks"]]

    assert refs.index("plan_loop") < refs.index("fan_out")
    assert [task["taskReferenceName"] for task in workflow["tasks"]
            if task["type"] == "DO_WHILE"] == ["plan_loop"]

    dumped = json.dumps(workflow)
    for forbidden in (
        "testCmd",
        "testCommands",
        "openspec_run_subtask_check",
        "openspec_validate_change",
        "test_discover",
        "test_run",
        "heavyweight verification",
    ):
        assert forbidden not in dumped

    delegated = [task for task in workflow["tasks"]
                 if task.get("subWorkflowParam", {}).get("name") == "test_cycle"]
    assert len(delegated) == 1
    assert delegated[0]["optional"] is True
    assert delegated[0]["inputParameters"]["testMode"] == "targeted"
    # After the merge produces a candidate, and before the handoff reports it.
    assert refs.index("merge") < refs.index("post_merge_tests") < refs.index("source_handoff")


def test_a_post_merge_fix_commit_reaches_the_handoff_head_guard():
    """The fix loop can advance the candidate past the merge commit.

    source_handoff asserts the checkout head equals workflow.variables.candidateCommit,
    so a tested-and-repaired SHA that never reaches that variable presents as
    drift and silently blocks publication.
    """
    workflow = _load("code_parallel")
    refs = [task["taskReferenceName"] for task in workflow["tasks"]]
    assert refs.index("post_merge_tests") < refs.index("verification_outcome") \
        < refs.index("verification_candidate_final") < refs.index("source_handoff")

    final = _find(workflow, "verification_candidate_final")
    assert final["type"] == "SET_VARIABLE"
    # Sourced from the jq fold, not straight from the optional child: a degraded
    # child returns null and would otherwise clobber a good SHA.
    assert final["inputParameters"]["candidateCommit"] == \
        "${verification_outcome.output.candidateCommit}"
    assert _find(workflow, "source_handoff")["inputParameters"]["expectedHead"] == \
        "${workflow.variables.candidateCommit}"


def test_post_merge_candidate_fold_survives_a_degraded_test_child():
    from common import code_parallel

    base = dict(candidate_commit="merged-sha", delivery={"state": "passed"}, issues=[],
               merge_state="merged", plan_valid=True, tests_passed=True)

    repaired = code_parallel.resolve_verification_outcome(
        **base, tested="repaired-sha", test_state="tests_passed")
    assert repaired["candidateCommit"] == "repaired-sha"
    assert repaired["testCycleState"] == "tests_passed"

    degraded = code_parallel.resolve_verification_outcome(
        **base, tested=None, test_state=None)
    assert degraded["candidateCommit"] == "merged-sha"
    assert degraded["testCycleState"] == "not_run"
    # tests_passed=True is still the caller's input, but the outcome only ever
    # trusts a testsPassed of True taken at face value alongside a fresh state.
    assert degraded["testsPassed"] is True


def test_plan_loop_validates_then_repairs_and_rechecks():
    workflow = _load("code_parallel")
    loop = _find(workflow, "plan_loop")
    refs = [task["taskReferenceName"] for task in loop["loopOver"]]

    # normalize_plan_check is gone: plan_check (verification_discover) now
    # folds the fallback-to-current-plan logic in directly.
    assert refs == ["plan_check", "plan_round_state", "plan_action"]
    assert _find(workflow, "plan_check")["name"] == "verification_discover"
    assert _find(workflow, "plan_check")["inputParameters"]["currentSubtasks"] == \
        "${workflow.variables.planSubtasks}"
    assert _find(workflow, "plan_check")["inputParameters"]["exhausted"] is False
    assert _find(workflow, "plan_check")["inputParameters"]["subtasks"] ==         "${workflow.variables.planSubtasks}"

    repair = _find(workflow, "plan_repair")
    assert repair["inputParameters"]["modelRole"] == "plan"
    assert repair["inputParameters"]["allowedTools"] == ["Read", "Grep", "Glob"]
    assert "Do not add test commands" in repair["inputParameters"]["prompt"]

    capture = _find(workflow, "capture_repaired_plan")
    assert capture["inputParameters"]["planSubtasks"] ==         "${plan_repair.output.structured.subtasks}"
    assert capture["inputParameters"]["planValid"] is False
    assert "plan_valid !== true" in loop["loopCondition"]


def test_final_repair_round_is_validated_after_the_loop_exhausts():
    """The loop's last iteration repairs without rechecking.

    ``loopCondition`` is evaluated after the body runs, so on the exhaustion
    path ``plan_repair`` produces a plan that no ``plan_check`` ever inspects
    and ``capture_repaired_plan`` leaves ``planValid`` false.  Without a
    post-loop check that plan is discarded and reported ``plan_rejected`` even
    when the repair fixed every issue.
    """
    workflow = _load("code_parallel")
    refs = [task["taskReferenceName"] for task in workflow["tasks"]]

    assert refs.index("plan_loop") < refs.index("final_plan_check")
    assert refs.index("final_plan_check") < refs.index("plan_gate")

    check = _find(workflow, "final_plan_check")
    assert check["name"] == "verification_discover"
    assert check["inputParameters"]["subtasks"] == "${workflow.variables.planSubtasks}"
    assert check["inputParameters"]["exhausted"] is True

    # plan_gate now routes on the variable directly, so the final verdict must
    # still be written back for it to read.
    state = _find(workflow, "final_plan_state")
    assert state["type"] == "SET_VARIABLE"
    assert state["inputParameters"]["planValid"] == "${final_plan_check.output.valid}"
    assert state["inputParameters"]["planSubtasks"] == "${final_plan_check.output.subtasks}"
    assert state["inputParameters"]["planIssues"] == "${final_plan_check.output.issues}"


def test_final_plan_check_promotes_a_repair_the_loop_never_rechecked():
    from common import plan_validation

    current = [{"id": "a", "description": "A", "files": ["a.py"]}]
    repaired = [{"id": "a", "description": "A", "files": ["src/a.py"]}]

    promoted = plan_validation.inspect_subtasks(repaired, "", current=current, exhausted=True)
    assert promoted["valid"] is True
    assert promoted["planningState"] == "valid"
    assert promoted["subtasks"] == repaired

    # Exhaustion is named for what happened; replanning is over, not pending.
    # The overlapping-file plan below is invalid, so the fallback is exercised.
    exhausted = plan_validation.inspect_subtasks(
        [{"id": "a", "description": "A", "files": ["a.py"]},
         {"id": "b", "description": "B", "files": ["a.py"]}],
        "", current=current, exhausted=True)
    assert exhausted["valid"] is False
    assert exhausted["planningState"] == "plan_exhausted"
    assert exhausted["subtasks"] == current


def test_precomputed_plan_bypasses_openspec_and_document_planners():
    workflow = _load("code_parallel")
    route = _find(workflow, "plan_route")

    assert route["evaluatorType"] == "graaljs"
    assert "precomputed" in route["expression"]
    branch = route["decisionCases"]["precomputed"]
    assert [task["taskReferenceName"] for task in branch] == ["set_precomputed_plan"]
    assert branch[0]["inputParameters"]["planSubtasks"] == "${workflow.input.precomputedPlan.subtasks}"


def test_normalize_plan_check_keeps_invalid_candidate_for_repair_feedback():
    from common import plan_validation

    current = [{"id": "a", "description": "A", "files": ["a.py"]}]

    invalid = plan_validation.inspect_subtasks(
        [{"id": "a", "description": "A", "files": ["a.py"]},
         {"id": "b", "description": "B", "files": ["a.py"]}],
        "", current=current, exhausted=False)
    assert invalid["valid"] is False
    assert invalid["planningState"] == "needs_replan"
    assert invalid["subtasks"] == current

    normalized = [{"id": "a", "description": "A", "files": ["src/a.py"]}]
    valid = plan_validation.inspect_subtasks(normalized, "", current=current, exhausted=False)
    assert valid["valid"] is True
    assert valid["planningState"] == "valid"
    assert valid["subtasks"] == normalized


def test_build_forks_consumes_only_plan_fields():
    from common import code_parallel

    result = code_parallel.build_forks(
        repo_path="/tmp/repo", code_model="model", code_prompt_template="",
        code_prompt_template_source="", max_turns=4, max_budget_usd=1,
        change_dir="design context", spec_context_path="", context_paths=[],
        model_profile="default", model_policy={}, model_policy_source="",
        model_policy_sha256="", models_config={}, model_overrides={},
        subtasks=[{
            "id": "api", "description": "Implement API", "files": ["src/api.py"],
            # testCmd is stripped by plan_validation before this ever runs, but
            # the function must not leak it into the prompt if it arrives anyway.
            "testCmd": "make integration-test",
        }])

    assert result["dynamicTasks"][0]["taskReferenceName"] == "api"
    branch_input = result["dynamicTasksInput"]["api"]
    assert branch_input["allowedWriteRoots"] == ["src/api.py"]
    assert "make integration-test" not in branch_input["prompt"]
    assert "testCmd" not in branch_input
    assert result["allowedWriteRoots"] == ["src/api.py"]


def test_rejected_plan_completes_child_with_explicit_non_publishable_state():
    workflow = _load("code_parallel")
    gate = _find(workflow, "plan_gate")
    terminate = gate["decisionCases"]["plan_rejected"][0]

    assert terminate["type"] == "TERMINATE"
    output = terminate["inputParameters"]["workflowOutput"]
    assert output["agentCompleted"] is False
    assert output["verificationState"] == "plan_rejected"
    assert output["verified"] is False
