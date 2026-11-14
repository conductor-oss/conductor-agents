"""Composition tests for test_cycle, the reusable run-tests-and-fix sub-workflow.

Two properties are load-bearing and easy to break by reordering tasks:

* the loop body is fix-then-verify, so the final test run is always a verdict
  and no fix is ever left unvalidated;
* the workflow completes for every terminal condition, so a caller can branch
  on ``testCycleState`` instead of on task failure.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WF = Path(__file__).resolve().parents[1] / "workflows"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq binary not installed")

# repair_scope is gone: test_discover resolves the write scope itself, since it
# is what computes repairRoots in the first place.
LOOP_BODY = [
    "repair_context", "repair", "repair_commit", "repair_result",
    "discover", "verification_plan", "verify", "repair_history", "record_iteration",
]
TERMINAL_STATES = {
    "tests_passed", "no_tests_required", "no_tests_found", "tests_failed_after_fix_budget",
    "tests_failed_fix_unavailable", "runtime_unavailable", "candidate_commit_missing",
    "verification_input_invalid", "verifier_worker_unavailable",
}


def _load(name: str) -> dict:
    return json.loads((WF / f"{name}.json").read_text())


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _find(workflow: dict, ref: str) -> dict:
    for node in _walk(workflow):
        if node.get("taskReferenceName") == ref:
            return node
    raise AssertionError(f"task {ref!r} not found")


def _eval_jq(query: str, data: dict, tmp_path: Path) -> dict:
    program = tmp_path / "program.jq"
    program.write_text(query, encoding="utf-8")
    proc = subprocess.run(["jq", "-f", str(program)], input=json.dumps(data),
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_model_policy_resolves_first():
    workflow = _load("test_cycle")
    assert workflow["tasks"][0]["taskReferenceName"] == "model_policy"
    assert workflow["tasks"][0]["name"] == "model_profile_resolve"


def test_no_task_in_the_workflow_can_ever_fail_it():
    """The never-fail guarantee, enforced structurally rather than by argument.

    Measured on this server: an unprotected ``JSON_JQ_TRANSFORM`` that throws
    ends the workflow FAILED, and so does a ``DO_WHILE`` whose ``graaljs``
    condition throws; both COMPLETE when marked optional. A ``SWITCH`` routes
    rather than executes and completes even when its expression names an absent
    input, so it is the one task type that needs no guard.

    Any new task added here without ``optional: true`` silently reopens the
    hole, which is why this asserts over the whole tree rather than a list.
    """
    workflow = _load("test_cycle")

    unprotected = [(task["type"], task["taskReferenceName"]) for task in _walk(workflow)
                   if task.get("taskReferenceName") and not task.get("optional")]
    assert [ref for kind, ref in unprotected if kind != "SWITCH"] == []

    # A TERMINATE is the other way to end non-COMPLETED, and there is none.
    assert not [task for task in _walk(workflow) if task.get("type") == "TERMINATE"]
    # No failureWorkflow, and no deadline that could yield TIMED_OUT.
    assert "failureWorkflow" not in workflow
    assert workflow["timeoutSeconds"] == 0
    assert workflow["timeoutPolicy"] == "ALERT_ONLY"
    # An unmatched outcome must fall through, never error.
    assert _find(workflow, "fix_needed_gate")["defaultCase"] == []


def test_every_output_survives_its_producer_degrading():
    """optional tasks return null, so the outputs must not depend on success."""
    workflow = _load("test_cycle")
    outputs = workflow["outputParameters"]
    # Loop-carried state comes from workflow variables, which always exist
    # because cycle_init seeds every one of them.
    seeded = set(workflow["variables"])
    for name, expression in outputs.items():
        if expression.startswith("${workflow.variables."):
            assert expression[len("${workflow.variables."):-1].split(".")[0] in seeded, name
    # Everything else reads the terminal normalizer, whose expression is
    # null-safe for every input.
    other = {name for name, expression in outputs.items()
             if not expression.startswith("${workflow.variables.")}
    assert all(outputs[name].startswith("${cycle_outcome.") or
               outputs[name].startswith("${initial_plan.") for name in other), other


def test_the_last_step_of_a_fix_iteration_is_a_test_run():
    workflow = _load("test_cycle")
    loop = _find(workflow, "test_fix_loop")
    body = [task["taskReferenceName"] for task in loop["loopOver"]]
    assert body == LOOP_BODY

    # Fix-then-verify is what makes the budget honest: loopCondition is
    # evaluated after the body, so the run that ends the loop is a verdict on
    # the last fix, never an unvalidated fix. The two bookkeeping steps that
    # follow it record the result; they do not test anything.
    names = [task["name"] for task in loop["loopOver"]]
    assert names.index("test_run") > names.index("coding_agent")
    assert names[names.index("test_run") + 1:] == ["test_cycle_progress", "record_repair_iteration"]


def test_budget_is_five_runs_and_four_fixes_and_stops_when_a_fix_stalls():
    workflow = _load("test_cycle")
    condition = _find(workflow, "test_fix_loop")["loopCondition"]

    # The literal bound and the operator input must both survive: an audit
    # mutation probe removes each in turn to prove the auditor still detects an
    # unbounded loop.
    assert "iteration'] < 4" in condition
    assert "maxFixAttempts" in condition
    assert "code_failed" in condition
    assert "candidateAdvanced === true" in condition
    assert _find(workflow, "test_fix_loop")["inputParameters"]["maxFixAttempts"] == \
        "${workflow.input.maxFixAttempts}"
    assert _load("test_cycle")["inputTemplate"]["maxFixAttempts"] == 4


def test_every_worker_outcome_maps_to_a_named_terminal_state():
    from common import test_plan

    def state(outcome, **overrides):
        data = {"outcome": outcome, "candidate": "abc", "advanced": True, "attempts": 0,
                "runs": 1, "source": "build_system_inference",
                "verification": {"commands": []}, "mode": "targeted", **overrides}
        return test_plan.cycle_outcome(**data)["testCycleState"]

    # Discovery never blocks: nothing to run is a named, passed state; an input the caller wired
    # wrong is named too. Any other value means the verifier is degraded.
    assert state("passed") == "tests_passed"
    assert state("no_tests_required") == "no_tests_required"
    assert state("no_tests_found") == "no_tests_found"
    assert state("code_failed") == "tests_failed_after_fix_budget"
    assert state("code_failed", advanced=False) == "tests_failed_fix_unavailable"
    assert state("invalid_input") == "verification_input_invalid"
    assert state("invalid_input", candidate="") == "candidate_commit_missing"
    assert state("infra_blocked") == "runtime_unavailable"
    assert state("cancelled") == "runtime_unavailable"
    assert state(None) == "verifier_worker_unavailable"
    assert state("pending") == "verifier_worker_unavailable"


def test_declared_terminal_states_match_the_documented_contract():
    from common import test_plan

    produced = set()
    for outcome in ("passed", "no_tests_required", "no_tests_found", "code_failed", "invalid_input",
                    "infra_blocked", "cancelled", None):
        for advanced in (True, False):
            for candidate in ("abc", ""):
                produced.add(test_plan.cycle_outcome(
                    outcome=outcome, candidate=candidate, advanced=advanced,
                    attempts=0, runs=1, source="none",
                    verification={"commands": []}, mode="targeted")["testCycleState"])
    assert produced == TERMINAL_STATES

    document = (Path(__file__).resolve().parents[2] / "docs" / "workflow-inputs.md").read_text()
    section = document.split("## `test_cycle` (internal)", 1)[1].split("\n## ", 1)[0]
    for state in TERMINAL_STATES:
        assert f"`{state}`" in section, state


def test_user_supplied_commands_outrank_every_discovered_source():
    from common import test_plan

    chosen = test_plan.resolve_plan(
        discovered=[{"argv": ["go", "test", "./x"], "source": "build-metadata:go"}],
        prior=[], user=[["pytest", "b.py"]],
        discovery_outcome="discovered", discovery_reason=None, selection="changed-scope")
    assert chosen["commandSource"] == "user_template"
    assert [command["argv"] for command in chosen["commands"]] == [["pytest", "b.py"]]

    for selection, source, expected in (
        ("documented-guide", "AGENTS.md", "repository_guide"),
        ("changed-scope", "repository-config:.conductor-code/verification.json", "repository_config"),
        ("changed-scope", "build-metadata:go", "build_system_inference"),
    ):
        result = test_plan.resolve_plan(
            discovered=[{"argv": ["go", "test", "./x"], "source": source}],
            prior=[], user=[], discovery_outcome="discovered",
            discovery_reason=None, selection=selection)
        assert result["commandSource"] == expected, selection


def test_mode_and_heavy_suite_inputs_reach_both_worker_tasks():
    workflow = _load("test_cycle")
    for ref in ("initial_discover", "initial_verify", "discover", "verify"):
        inputs = _find(workflow, ref)["inputParameters"]
        assert inputs["testMode"] == "${workflow.input.testMode}", ref
        assert inputs["allowHeavySuites"] == "${workflow.input.allowHeavySuites}", ref


def test_legacy_output_names_survive_so_existing_callers_need_no_edit():
    outputs = _load("test_cycle")["outputParameters"]
    for legacy in ("verificationState", "candidateCommit", "verification", "executionOutcome",
                   "requiredVerificationCommands", "repairRoots", "repairReports", "attempts"):
        assert legacy in outputs, legacy
    for added in ("testCycleState", "testsPassed", "testsRan", "testRunCount",
                  "fixAttempts", "testReport", "commandSource", "testMode"):
        assert added in outputs, added


def test_discovery_feeds_planning_and_the_initial_run_directly():
    # Discovery never blocks, so nothing sits between it and the plan: no fallback sub-workflow, no
    # variable seeded by one. The initial run's repair scope is the discovery's own.
    workflow = _load("test_cycle")
    refs = [task["taskReferenceName"] for task in workflow["tasks"]]
    assert refs.index("initial_discover") + 1 == refs.index("initial_plan")
    assert "agent_fallback" not in refs and "allowAgentTestPlan" not in json.dumps(workflow)
    record_initial_run = _find(workflow, "record_initial_run")
    assert record_initial_run["inputParameters"]["repairRoots"] == "${initial_discover.output.repairRoots}"


def test_registration_lists_test_cycle_before_every_caller():
    order = (Path(__file__).resolve().parents[1] / "register.sh").read_text()
    line = next(item for item in order.splitlines() if item.startswith("WORKFLOW_ORDER=("))
    names = line.removeprefix("WORKFLOW_ORDER=(").rstrip(")").split()
    assert "test_cycle" in names, "an unregistered sub-workflow waits forever at its first task"
    for caller in ("code_subtask", "code_parallel", "feature_campaign",
                   "openspec_development", "github_demo", "issue_to_pr", "address_pr_repair"):
        assert names.index("test_cycle") < names.index(caller), caller
