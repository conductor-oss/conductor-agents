"""Composition tests for test_agent_fallback, extracted from test_cycle to keep
that workflow's own graph small: this is test_cycle's agent-assisted last
resort when deterministic discovery is configuration_blocked (tier 1: a
read-only content search; tier 2, only if tier 1 also finds nothing: an agent
authors and red/green-checks exactly one new test file).
"""

from __future__ import annotations

import json
from pathlib import Path

WF = Path(__file__).resolve().parents[1] / "workflows"


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


def test_agent_fallback_gate_requires_the_flag_and_a_configuration_block():
    workflow = _load("test_agent_fallback")
    assert workflow["inputTemplate"]["allowAgentTestPlan"] is False

    gate = _find(workflow, "agent_fallback_gate")
    assert gate["type"] == "SWITCH"
    assert gate["evaluatorType"] == "graaljs"
    assert "allow" in gate["expression"] and "configuration_blocked" in gate["expression"]
    assert gate["inputParameters"]["allow"] == "${workflow.input.allowAgentTestPlan}"
    assert gate["inputParameters"]["outcome"] == "${workflow.input.discoveryOutcome}"
    assert gate["defaultCase"] == []

    propose_branch = gate["decisionCases"]["propose"]
    branch_refs = [task["taskReferenceName"] for task in propose_branch]
    assert branch_refs == ["propose_commands", "agent_plan", "record_agent_plan", "author_test_gate"]
    for task in propose_branch:
        assert task.get("optional") is True, task["taskReferenceName"]

    propose = _find(workflow, "propose_commands")
    assert propose["inputParameters"]["tools"] == ["Read", "Grep", "Glob"]
    assert propose["inputParameters"]["failSoft"] is True


def test_resolves_its_own_model_policy_rather_than_reusing_the_caller():
    # A repo-wide invariant (test_model_profile_propagation.py): every
    # workflow resolves its own model policy locally, and every coding_agent
    # task's modelResolution is ${model_policy.output} -- never a
    # passed-through parent value.
    workflow = _load("test_agent_fallback")
    refs = [task["taskReferenceName"] for task in workflow["tasks"]]
    assert refs[0] == "model_policy"
    for ref in ("propose_commands", "author_missing_test"):
        assert _find(workflow, ref)["inputParameters"]["modelResolution"] == "${model_policy.output}"


def test_fallback_init_seeds_the_pass_through_baseline():
    # Makes the whole sub-workflow a true no-op when the gate skips: these
    # variables must already equal the caller's inputs before the gate ever
    # runs, since outputParameters always reads from them.
    workflow = _load("test_agent_fallback")
    refs = [task["taskReferenceName"] for task in workflow["tasks"]]
    assert refs.index("fallback_init") < refs.index("agent_fallback_gate")

    init = _find(workflow, "fallback_init")
    assert init["type"] == "SET_VARIABLE"
    assert init["inputParameters"] == {
        "candidateCommit": "${workflow.input.candidateCommit}",
        "repairRoots": "${workflow.input.repairRoots}",
    }


def test_author_test_gate_requires_the_new_flag_and_an_empty_agent_plan():
    workflow = _load("test_agent_fallback")
    assert workflow["inputTemplate"]["allowAgentAuthoredTests"] is False

    gate = _find(workflow, "author_test_gate")
    assert gate["type"] == "SWITCH"
    assert gate["evaluatorType"] == "graaljs"
    assert "allowAuthor" in gate["expression"] and "candidates" in gate["expression"]
    assert gate["inputParameters"]["allowAuthor"] == "${workflow.input.allowAgentAuthoredTests}"
    assert gate["inputParameters"]["candidates"] == "${agent_plan.output.candidates}"
    assert gate["defaultCase"] == []


def test_author_missing_test_cannot_run_commands_and_is_pinned_to_claude():
    author = _find(_load("test_agent_fallback"), "author_missing_test")
    assert author["inputParameters"]["tools"] == ["Read", "Grep", "Glob", "Write", "Edit"]
    assert "Bash" not in author["inputParameters"]["tools"]
    assert author["inputParameters"]["agent"] == "claude"
    assert "allowedWriteRoots" not in author["inputParameters"]
    assert author.get("optional") is True


def test_authored_shape_gate_discards_a_rejected_attempt_and_commits_an_accepted_one():
    workflow = _load("test_agent_fallback")
    gate = _find(workflow, "authored_shape_gate")
    assert gate["evaluatorType"] == "value-param"
    assert gate["expression"] == "accepted"
    assert gate["inputParameters"]["accepted"] == "${authored_shape.output.accepted}"

    default_refs = [task["taskReferenceName"] for task in gate["defaultCase"]]
    assert default_refs == ["authored_discard"]
    discard = _find(workflow, "authored_discard")
    assert discard["inputParameters"]["paths"] == "${authored_shape.output.touchedPaths}"

    true_refs = [task["taskReferenceName"] for task in gate["decisionCases"]["true"]]
    assert true_refs == ["authored_commit_verify", "authored_accepted_gate"]


def test_authored_commit_verify_commits_discovers_and_redgreen_checks_in_one_task():
    # Replaces what used to be three tasks (commit, test_discover,
    # test_authored_test_redgreen) plus a SWITCH between them -- the
    # intermediate states had no independent value to this workflow, only
    # the final accepted/rejected verdict does. See
    # verification.commit_and_verify_authored_test for the mechanism,
    # including the automatic rollback on rejection.
    workflow = _load("test_agent_fallback")
    commit_verify = _find(workflow, "authored_commit_verify")
    assert commit_verify["name"] == "test_authored_test_commit_verify"
    assert commit_verify["inputParameters"]["preCandidateCommit"] == "${workflow.variables.candidateCommit}"
    assert commit_verify["inputParameters"]["testMode"] == "${workflow.input.testMode}"
    assert commit_verify.get("optional") is True


def test_record_authored_test_only_fires_when_commit_verify_accepts():
    workflow = _load("test_agent_fallback")
    accepted_gate = _find(workflow, "authored_accepted_gate")
    assert accepted_gate["evaluatorType"] == "value-param"
    assert accepted_gate["expression"] == "accepted"
    assert accepted_gate["inputParameters"]["accepted"] == "${authored_commit_verify.output.accepted}"
    assert accepted_gate["defaultCase"] == []
    assert [task["taskReferenceName"] for task in accepted_gate["decisionCases"]["true"]] == ["record_authored_test"]

    record = _find(workflow, "record_authored_test")
    assert record["type"] == "SET_VARIABLE"
    assert record["inputParameters"]["candidateCommit"] == "${authored_commit_verify.output.commit}"
    assert record["inputParameters"]["agentCommands"] == "${authored_commit_verify.output.commands}"
    assert record["inputParameters"]["repairRoots"] == "${authored_commit_verify.output.repairRoots}"
    assert record["inputParameters"]["agentAuthoredTest"] is True
    assert record["inputParameters"]["agentAuthoredTestPath"] == "${authored_shape.output.authoredPath}"


def test_output_parameters_always_read_from_the_pass_through_variables():
    workflow = _load("test_agent_fallback")
    assert workflow["outputParameters"] == {
        "agentCommands": "${workflow.variables.agentCommands}",
        "candidateCommit": "${workflow.variables.candidateCommit}",
        "repairRoots": "${workflow.variables.repairRoots}",
        "agentAuthoredTest": "${workflow.variables.agentAuthoredTest}",
        "agentAuthoredTestPath": "${workflow.variables.agentAuthoredTestPath}",
    }


def test_every_task_is_optional_so_this_workflow_cannot_fail_its_caller():
    workflow = _load("test_agent_fallback")
    for node in _walk(workflow.get("tasks", [])):
        if "taskReferenceName" not in node or node.get("type") in (
                "SWITCH", "DO_WHILE", "FORK_JOIN", "FORK_JOIN_DYNAMIC", "JOIN"):
            continue
        assert node.get("optional") is True, node.get("taskReferenceName")
