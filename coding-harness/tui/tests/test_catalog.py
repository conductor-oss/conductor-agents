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
    assert payload["planAgent"] == "codex" and payload["codeAgent"] == "codex"
    # unchanged defaults are omitted
    assert "base" not in payload and "maxTurns" not in payload and "design" not in payload


def test_target_and_result_helpers():
    assert "acme/app" in catalog.target_for("pr_review", {"repo": "https://github.com/acme/app.git", "prNumber": 7})
    card = catalog.result_for("pr_review", {"reviewUrl": "http://x/pull/7", "event": "COMMENT", "inlineCount": 2})
    assert card and card.primary_url == "http://x/pull/7"
    assert catalog.short_repo("git@github.com:acme/app.git") == "acme/app"


def test_local_checkout_paths_are_expanded_before_start(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    normalized = catalog.normalize_local_paths({"repoPath": "~/src/app"})
    assert normalized["repoPath"] == str((tmp_path / "src" / "app").resolve())
    normalized = catalog.normalize_local_paths({"specSource": "~/src/app/design/openspec"})
    assert normalized["specSource"] == str((tmp_path / "src" / "app" / "design" / "openspec").resolve())


def test_design_docs_is_bounded_review_loop():
    wf = _load("design_docs")
    assert wf["inputTemplate"]["humanApproval"] is True
    assert wf["inputTemplate"]["designMaxIterations"] == 5
    loop = next(t for t in wf["tasks"] if t["taskReferenceName"] == "design_loop")
    assert loop["type"] == "DO_WHILE" and loop["evaluatorType"] == "graaljs"
    assert "$.design_loop['iteration'] < $.max_iterations" in loop["loopCondition"]
    review = next(t for t in loop["loopOver"] if t["taskReferenceName"] == "review_mode")
    assert any(t["type"] == "WAIT" for t in review["decisionCases"]["true"])
    judge = next(t for t in review["defaultCase"] if t["taskReferenceName"] == "design_judge")
    assert judge["type"] == "SIMPLE" and judge["name"] == "coding_agent"
    assert judge["inputParameters"]["tools"] == ["Read", "Grep", "Glob"]
    assert judge["inputParameters"]["maxTurns"] == "${workflow.input.designMaxTurns}"
    assert judge["inputParameters"]["maxBudgetUsd"] == "${workflow.input.designMaxBudgetUsd}"
    assert set(judge["inputParameters"]["schema"]["required"]) == {"approved", "feedback"}
    assert "${design.output.result}" in judge["inputParameters"]["prompt"]
    set_review = next(t for t in review["defaultCase"] if t["taskReferenceName"] == "set_judge_review")
    assert set_review["inputParameters"]["designApproved"] == "${design_judge.output.structured.approved}"
    assert set_review["inputParameters"]["designFeedback"] == "${design_judge.output.structured.feedback}"
    assert set_review["inputParameters"]["designReview"] == "agent"


@pytest.mark.parametrize(
    "wf_name", ["pr_review", "issue_to_pr", "design_docs", "feature_campaign"]
)
def test_interactive_checkpoints_use_signalable_wait_tasks(wf_name):
    tasks = list(_walk_tasks(_load(wf_name)["tasks"]))
    assert not [task for task in tasks if task.get("type") == "HUMAN"]


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
def test_code_parallel_paths_forward_design_review_controls(wf_name):
    wf = _load(wf_name)
    template = wf["inputTemplate"]
    assert template["design"] is False and template["designHumanApproval"] is True
    assert template["designMaxIterations"] == 5
    serialized = json.dumps(wf)
    for key in ("designHumanApproval", "designMaxIterations"):
        assert f"${{workflow.input.{key}}}" in serialized


@pytest.mark.parametrize(
    ("wf_name", "budget_key"),
    [
        ("address_pr", "maxBudgetUsd"),
        ("code_parallel", "maxBudgetUsd"),
        ("code_subtask", "maxBudgetUsd"),
        ("design_docs", "designMaxBudgetUsd"),
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
        ("code_parallel", "designMaxTurns", 500),
        ("code_parallel", "planMaxTurns", 500),
        ("code_parallel", "maxTurns", 500),
        ("code_subtask", "maxTurns", 250),
        ("design_docs", "designMaxTurns", 500),
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
    plan = next(t for t in _walk_tasks(wf["tasks"]) if t.get("taskReferenceName") == "plan")
    merge = next(t for t in wf["tasks"] if t["taskReferenceName"] == "merge")
    assert plan["inputParameters"]["maxBudgetUsd"] == 50.0
    assert merge["inputParameters"]["maxBudgetUsd"] == "${workflow.input.maxBudgetUsd}"


def _walk_tasks(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_tasks(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_tasks(child)
