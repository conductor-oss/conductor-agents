"""Compatibility tests for every JSON_JQ_TRANSFORM in the workflow catalog.

The integration test uses a normal Conductor workflow execution.  It does not
evaluate expressions with the host jq binary and does not mock any JQ task.
Set RUN_CONDUCTOR_INTEGRATION=1 to run it against CONDUCTOR_SERVER_URL.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "workflows"
CASES_FILES = (
    Path(__file__).resolve().parent / "fixtures" / "jq_conductor_cases.json",
    Path(__file__).resolve().parent / "fixtures" / "jq_conductor_adversarial_cases.json",
)
THIRD_CASES_FILE = (
    Path(__file__).resolve().parent / "fixtures" / "jq_conductor_third_pass_cases.json"
)
BASE = os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080/api").rstrip("/")
TOKEN = os.environ.get("CONDUCTOR_AUTH_TOKEN", "")
TEST_WORKFLOW = "coding_harness_jq_conductor_compatibility"
THIRD_TEST_WORKFLOW = "coding_harness_jq_conductor_compatibility_third_pass"
TERMINAL = {"COMPLETED", "FAILED", "TIMED_OUT", "TERMINATED"}


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _catalog() -> dict[tuple[str, str], dict]:
    found = {}
    for path in sorted(WORKFLOW_DIR.glob("*.json")):
        workflow = json.loads(path.read_text(encoding="utf-8"))
        for task in _walk(workflow):
            if task.get("type") == "JSON_JQ_TRANSFORM":
                key = (workflow["name"], task["taskReferenceName"])
                assert key not in found, f"duplicate JQ task key: {key}"
                found[key] = task
    return found


def _load_cases(paths) -> list[dict]:
    cases = []
    for path in paths:
        cases.extend(json.loads(path.read_text(encoding="utf-8")))
    return cases


def _cases() -> list[dict]:
    return _load_cases(CASES_FILES)


def _third_cases() -> list[dict]:
    return _load_cases((THIRD_CASES_FILE,))


def _all_cases() -> list[dict]:
    return _cases() + _third_cases()


def _request(method: str, path: str, body=None):
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["X-Authorization"] = TOKEN
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(request) as response:  # noqa: S310
        raw = response.read().decode()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw


def _delete_definition(workflow_name: str) -> None:
    try:
        _request("GET", f"/metadata/workflow/{workflow_name}?version=1")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        raise
    _request("DELETE", f"/metadata/workflow/{workflow_name}/1")


def _result_at(result, path: str):
    current = result
    if not path:
        return current
    for part in path.split("."):
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def _assert_expectations(case: dict, result) -> None:
    for expectation in case["expect"]:
        actual = _result_at(result, expectation["path"])
        if "equals" in expectation:
            expected = expectation["equals"]
            if isinstance(expected, float):
                assert actual == pytest.approx(expected)
            else:
                assert actual == expected
        elif "contains" in expectation:
            expected = expectation["contains"]
            assert expected in actual
        else:
            expected = expectation["notContains"]
            assert expected not in actual


def _compatibility_ref(case: dict) -> str:
    return f"{case['workflow']}__{case['task']}__{case.get('case', 'primary')}"


def _test_definition(cases: list[dict], catalog: dict[tuple[str, str], dict],
                     workflow_name: str) -> dict:
    tasks = []
    for index, case in enumerate(cases, start=1):
        source = catalog[(case["workflow"], case["task"])]
        tasks.append({
            "name": f"jq_compatibility_{index:02d}",
            "taskReferenceName": _compatibility_ref(case),
            "type": "JSON_JQ_TRANSFORM",
            "inputParameters": {
                **case["input"],
                "queryExpression": source["inputParameters"]["queryExpression"],
            },
        })
    return {
        "name": workflow_name,
        "description": "Live compatibility execution for every coding-harness JQ task",
        "version": 1,
        "schemaVersion": 2,
        "inputParameters": [],
        "tasks": tasks,
        "outputParameters": {},
        "ownerEmail": "conductor@localhost",
    }


def test_jq_cases_cover_the_complete_workflow_catalog_with_adversarial_cases():
    catalog = _catalog()
    cases = _all_cases()
    keys = [(case["workflow"], case["task"]) for case in cases]
    assert set(keys) == set(catalog), (
        f"missing={sorted(set(catalog) - set(keys))}; extra={sorted(set(keys) - set(catalog))}"
    )
    identities = [(*key, case.get("case", "primary")) for key, case in zip(keys, cases)]
    assert len(identities) == len(set(identities)), "a named JQ compatibility case is duplicated"
    for key in catalog:
        assert keys.count(key) >= 3, f"missing third-pass coverage for {key[0]}/{key[1]}"
    assert len(cases) == len(catalog) * 3
    for case in cases:
        assert case["expect"], f"missing semantic assertion for {case['workflow']}/{case['task']}"


def test_adversarial_regressions_keep_explicit_null_and_blank_guards():
    """The repo's JQ catalog is empty now (see AGENTS.md's JQ Usage Policy), so
    every guard this test used to pin against a queryExpression string lives in
    a Python module instead, directly unit-tested and directly asserted here.
    """
    assert _catalog() == {}

    from common import (code_subtask, feature_campaign, issue_to_pr, openspec_artifact_drain,
                        openspec_generate_artifact, pr_description, publish_salvage, test_plan)

    # feature_campaign/implementation_done: a null remaining list or outcome
    # must not crash the loop-exit check.
    assert feature_campaign.is_implementation_done(outcome=None, remaining=None) is False
    assert feature_campaign.is_implementation_done(outcome=None, remaining=[]) is True

    # The PR-body guards moved out of jq entirely: pr_description.format_summary
    # owns the blank-body fallback and the one-section invariant now. See
    # test_pr_body_composition and test_pr_fork_workflows.
    assert pr_description.HEADING == "## Summary"

    # merge_worktrees names its own verdict now: anything left unresolved means
    # the merge did not complete. See test_gitops.

    # openspec_artifact_drain/merge_pass_progress: files accumulate only when
    # something was actually ready this pass, and a null prevFiles must not throw.
    progress = openspec_artifact_drain.merge_pass_progress(
        fan_output=None, ready_ids=[], prev_generated=None, prev_files=None,
        prev_cost=None, prev_tokens=None)
    assert progress["filesChanged"] == []

    # openspec_artifact_drain/select_ready: only "ready" artifacts fork, and an
    # already-generated one is skipped even if it is still reported ready.
    ready = openspec_artifact_drain.select_ready(
        status={"artifacts": [{"id": "a", "status": "ready"}, {"id": "b", "status": "blocked"},
                              {"id": "c", "status": "ready"}]},
        generated=["c"], repo_path="/r", change_name="x", feedback="", goal="",
        model="", max_turns=1, max_budget_usd=1, model_profile="", model_policy={},
        model_policy_source="", model_policy_sha256="", models_config="", model_overrides={})
    assert ready["readyIds"] == ["a"]

    # openspec_generate_artifact/build_prompt: blank feedback must not add an
    # empty "address every item" section.
    prompt = openspec_generate_artifact.build_prompt(
        instr={"artifactId": "x", "instruction": "i", "template": "t", "resolvedOutputPath": "p"},
        goal="g", feedback="   ")
    assert "address every item" not in prompt

    # publish_salvage/salvage_plan and salvage_outcome: blank recovered state
    # must not report publishable, and a non-numeric PR number is "no PR".
    plan = publish_salvage.build_salvage_plan({})
    assert plan["canPublish"] == "false"
    assert publish_salvage.resolve_salvage_outcome(None)["state"] == "pushed_no_pr"
    assert publish_salvage.resolve_salvage_outcome(7)["state"] == "salvaged"

    # code_subtask/delivery_outcome: a null commit/audit/agent must not throw.
    assert code_subtask.resolve_delivery_outcome(
        planned_paths=None, commit=None, audit=None, agent=None,
        tested=None, test_state=None, tests_passed=None)["state"]

    # issue_to_pr: a null delivery/verification must not throw, and a blank
    # commit falls through to the workspace head or the caller's fallback.
    assert issue_to_pr.normalize_issue_delivery(
        child=None, publication_commit=None, workspace_state=None, fallback_commit="fb"
    )["commit"] == "fb"

    # test_cycle's guards moved out of jq into common/test_plan.py, where every
    # function is total by construction and directly unit-tested. See
    # test_test_cycle_composition and test_test_plan.
    assert test_plan.resolve_plan(discovered=None, prior=None, user=None,
                                  discovery_outcome=None, discovery_reason=None,
                                  selection=None)["commands"] == []
    assert test_plan.repair_scope(["  "], ["src"]) == ["src"]
    assert test_plan.record_repair(current_candidate="old", agent={},
                                   commit={"noOp": False, "stagedPaths": ["a"],
                                           "commit": "  "})["candidate"] == "old"


@pytest.mark.skipif(
    os.environ.get("RUN_CONDUCTOR_INTEGRATION") != "1",
    reason="set RUN_CONDUCTOR_INTEGRATION=1 to execute all JQ tasks in live Conductor",
)
def test_registered_v1_jq_expressions_match_the_reviewed_source():
    local = _catalog()
    workflows = sorted({workflow for workflow, _task in local})
    registered = {}
    for workflow_name in workflows:
        definition = _request("GET", f"/metadata/workflow/{workflow_name}?version=1")
        assert definition["version"] == 1
        for task in _walk(definition):
            if task.get("type") == "JSON_JQ_TRANSFORM":
                registered[(workflow_name, task["taskReferenceName"])] = task

    assert set(registered) == set(local)
    for key, local_task in local.items():
        assert registered[key]["inputParameters"]["queryExpression"] == (
            local_task["inputParameters"]["queryExpression"]
        ), f"registered v1 expression is stale for {key[0]}/{key[1]}"


def _execute_live_cases(cases: list[dict], workflow_name: str) -> None:
    catalog = _catalog()
    definition = _test_definition(cases, catalog, workflow_name)
    _delete_definition(workflow_name)
    workflow_id = None
    try:
        _request("POST", "/metadata/workflow/validate", definition)
        _request("POST", "/metadata/workflow", definition)
        started = _request("POST", f"/workflow/{workflow_name}", {})
        workflow_id = started.get("workflowId") if isinstance(started, dict) else str(started)
        assert workflow_id

        deadline = time.monotonic() + 90
        execution = None
        while time.monotonic() < deadline:
            execution = _request("GET", f"/workflow/{workflow_id}?includeTasks=true")
            if execution["status"] in TERMINAL:
                break
            time.sleep(0.2)
        assert execution is not None
        assert execution["status"] == "COMPLETED", (
            workflow_id,
            execution.get("reasonForIncompletion"),
            [(task.get("referenceTaskName"), task.get("status"), task.get("reasonForIncompletion"))
             for task in execution.get("tasks", [])],
        )

        tasks = {task["referenceTaskName"]: task for task in execution["tasks"]}
        assert len(tasks) == len(cases)
        mismatches = []
        for case in cases:
            task = tasks[_compatibility_ref(case)]
            assert task["taskType"] == "JSON_JQ_TRANSFORM"
            assert task["status"] == "COMPLETED"
            assert task["taskId"]
            try:
                _assert_expectations(case, task["outputData"]["result"])
            except (AssertionError, KeyError, IndexError, TypeError) as exc:
                mismatches.append(
                    f"{case['workflow']}/{case['task']}[{case.get('case', 'primary')}]: "
                    f"{exc}; result={task['outputData'].get('result')!r}"
                )
        assert not mismatches, "JQ semantic mismatches:\n" + "\n".join(mismatches)

        print(f"REAL_CONDUCTOR_WORKFLOW_ID={workflow_id}")
        print(f"REAL_CONDUCTOR_JQ_TASKS={len(tasks)}")
    finally:
        _delete_definition(workflow_name)
        if workflow_id and os.environ.get("KEEP_CONDUCTOR_JQ_EXECUTION") != "1":
            try:
                _request("DELETE", f"/workflow/{workflow_id}/remove")
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise


@pytest.mark.skipif(
    os.environ.get("RUN_CONDUCTOR_INTEGRATION") != "1",
    reason="set RUN_CONDUCTOR_INTEGRATION=1 to execute all JQ tasks in live Conductor",
)
def test_every_jq_task_executes_in_real_conductor_with_expected_output():
    _execute_live_cases(_cases(), TEST_WORKFLOW)


@pytest.mark.skipif(
    os.environ.get("RUN_CONDUCTOR_INTEGRATION") != "1",
    reason="set RUN_CONDUCTOR_INTEGRATION=1 to execute all JQ tasks in live Conductor",
)
def test_every_jq_task_executes_a_third_adversarial_case_in_real_conductor():
    _execute_live_cases(_third_cases(), THIRD_TEST_WORKFLOW)
