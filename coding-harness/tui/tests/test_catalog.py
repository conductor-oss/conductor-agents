"""Catalog ↔ workflow-JSON drift test.

Guarantees the launcher forms never lie: every catalog field default must equal the
registered workflow's inputTemplate value, and every required field must be a real
workflow input with no server-side default.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from tui import catalog

WF_DIR = pathlib.Path(__file__).resolve().parents[2] / "workers" / "workflows"


def _load(name: str) -> dict:
    return json.loads((WF_DIR / f"{name}.json").read_text())


@pytest.mark.parametrize("wf_name", catalog.LAUNCHABLE)
def test_fields_match_workflow_defaults(wf_name):
    spec = catalog.CATALOG[wf_name]
    wf = _load(wf_name)
    params = set(wf.get("inputParameters") or [])
    template = wf.get("inputTemplate") or {}

    for f in spec.fields:
        for key in f.targets:
            assert key in params, f"{wf_name}: field {f.name!r} maps to {key!r} which is not a workflow input"
            if f.required:
                # required fields must NOT have a server-side default (else they'd be optional)
                assert key not in template, f"{wf_name}: {key!r} has a template default but is marked required"
            else:
                assert key in template, f"{wf_name}: optional field {f.name!r} → {key!r} missing from inputTemplate"
                assert template[key] == f.default, (
                    f"{wf_name}: default drift on {key!r}: "
                    f"catalog={f.default!r} vs workflow={template[key]!r}"
                )


@pytest.mark.parametrize("wf_name", catalog.LAUNCHABLE)
def test_every_required_workflow_input_is_covered(wf_name):
    """Each workflow's required inputs (in inputParameters but not inputTemplate) must
    have a catalog field, or the launcher couldn't produce a runnable payload."""
    spec = catalog.CATALOG[wf_name]
    wf = _load(wf_name)
    params = wf.get("inputParameters") or []
    template = wf.get("inputTemplate") or {}
    required_inputs = {p for p in params if p not in template}
    covered = {key for f in spec.fields for key in f.targets}
    missing = required_inputs - covered
    assert not missing, f"{wf_name}: required inputs not covered by a form field: {missing}"


def test_build_payload_omits_defaults_and_expands_maps_to():
    spec = catalog.CATALOG["issue_to_pr"]
    # user set repo/issue, changed backend to codex, left everything else default
    values = {f.name: f.default for f in spec.fields}
    values.update({"repo": "acme/app", "issueNumber": 42, "backend": "codex"})
    payload = spec.build_payload(values)
    assert payload["repo"] == "acme/app"
    assert payload["issueNumber"] == 42
    # backend expands to both plan + code agents
    assert payload["openspecPlanAgent"] == "codex" and payload["codeAgent"] == "codex"
    # unchanged defaults are omitted
    assert "base" not in payload and "maxTurns" not in payload


def test_target_and_result_helpers():
    assert "acme/app" in catalog.target_for("pr_review", {"repo": "https://github.com/acme/app.git", "prNumber": 7})
    card = catalog.result_for("pr_review", {"reviewUrl": "http://x/pull/7", "event": "COMMENT", "inlineCount": 2})
    assert card and card.primary_url == "http://x/pull/7"
    assert catalog.short_repo("git@github.com:acme/app.git") == "acme/app"


def test_openspec_plan_is_bounded_review_loop():
    wf = _load("openspec_plan")
    assert wf["inputTemplate"]["openspecHumanApproval"] is True
    assert wf["inputTemplate"]["openspecMaxIterations"] == 5
    loop = next(t for t in wf["tasks"] if t["taskReferenceName"] == "openspec_loop")
    assert loop["type"] == "DO_WHILE" and loop["evaluatorType"] == "graaljs"
    assert "$.openspec_loop['iteration'] < $.max_iterations" in loop["loopCondition"]
    review = next(t for t in loop["loopOver"] if t["taskReferenceName"] == "review_mode")
    assert any(t["type"] == "HUMAN" for t in review["decisionCases"]["true"])
    judge = next(t for t in review["defaultCase"] if t["taskReferenceName"] == "plan_judge")
    assert judge["type"] == "SIMPLE" and judge["name"] == "coding_agent"
    assert judge["inputParameters"]["tools"] == ["Read", "Grep", "Glob"]
    assert judge["inputParameters"]["maxTurns"] == "${workflow.input.openspecMaxTurns}"
    assert judge["inputParameters"]["maxBudgetUsd"] == "${workflow.input.openspecMaxBudgetUsd}"
    assert set(judge["inputParameters"]["schema"]["required"]) == {"approved", "feedback"}
    assert "${workflow.input.instruction}" in judge["inputParameters"]["prompt"]
    set_review = next(t for t in review["defaultCase"] if t["taskReferenceName"] == "set_judge_review")
    assert set_review["inputParameters"]["planApproved"] == "${plan_judge.output.structured.approved}"
    assert set_review["inputParameters"]["planFeedback"] == "${plan_judge.output.structured.feedback}"
    assert set_review["inputParameters"]["planReview"] == "agent"


def test_no_llm_chat_complete_tasks_remain():
    found = []

    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "LLM_CHAT_COMPLETE":
                found.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for path in WF_DIR.glob("*.json"):
        visit(json.loads(path.read_text()))

    assert not found


@pytest.mark.parametrize("wf_name", ["code_parallel", "issue_to_pr", "address_pr"])
def test_code_parallel_paths_forward_openspec_plan_controls(wf_name):
    wf = _load(wf_name)
    template = wf["inputTemplate"]
    assert template["openspecHumanApproval"] is True
    assert template["openspecMaxIterations"] == 5
    serialized = json.dumps(wf)
    for key in ("openspecHumanApproval", "openspecMaxIterations"):
        assert f"${{workflow.input.{key}}}" in serialized


@pytest.mark.parametrize(
    ("wf_name", "budget_key"),
    [
        ("address_pr", "maxBudgetUsd"),
        ("code_parallel", "maxBudgetUsd"),
        ("code_parallel", "openspecMaxBudgetUsd"),
        ("code_subtask", "maxBudgetUsd"),
        ("openspec_plan", "openspecMaxBudgetUsd"),
        ("openspec_generate_artifact", "maxBudgetUsd"),
        ("github_demo", "maxBudgetUsd"),
        ("issue_to_pr", "maxBudgetUsd"),
        ("pr_review", "maxBudgetUsd"),
    ],
)
def test_all_workflow_agent_budget_defaults_are_fifty(wf_name, budget_key):
    assert _load(wf_name)["inputTemplate"][budget_key] == 50.0


@pytest.mark.parametrize(
    ("wf_name", "turn_key", "expected"),
    [
        ("address_pr", "maxTurns", 250),
        ("code_parallel", "openspecMaxTurns", 500),
        ("code_parallel", "maxTurns", 500),
        ("code_subtask", "maxTurns", 250),
        ("openspec_plan", "openspecMaxTurns", 500),
        ("github_demo", "maxTurns", 300),
        ("issue_to_pr", "maxTurns", 300),
        ("pr_review", "maxTurns", 250),
    ],
)
def test_workflow_agent_turn_defaults(wf_name, turn_key, expected):
    value = _load(wf_name)["inputTemplate"][turn_key]
    assert value == expected
    assert value >= 25


def test_code_parallel_internal_agent_budgets_are_fifty():
    wf = _load("code_parallel")
    plan = next(t for t in wf["tasks"] if t["taskReferenceName"] == "openspec_plan")
    merge = next(t for t in wf["tasks"] if t["taskReferenceName"] == "merge")
    assert plan["inputParameters"]["openspecMaxBudgetUsd"] == "${workflow.input.openspecMaxBudgetUsd}"
    assert merge["inputParameters"]["maxBudgetUsd"] == "${workflow.input.maxBudgetUsd}"
