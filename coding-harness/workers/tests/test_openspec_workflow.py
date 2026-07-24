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


def test_openspec_workflow_contract_and_routes():
    wf = _load("openspec_development")
    assert wf["version"] == 2 and wf["schemaVersion"] == 2
    required = set(wf["inputParameters"]) - set(wf["inputTemplate"])
    assert required == {"specSource", "changeId"}
    assert wf["inputTemplate"]["useSpecSourceWorkspace"] is False
    assert wf["inputTemplate"]["repoPath"] == ""
    children = {node.get("subWorkflowParam", {}).get("name") for node in _walk(wf)
                if node.get("type") == "SUB_WORKFLOW"}
    assert {"code_parallel", "feature_campaign"}.issubset(children)
    serialized = json.dumps(wf)
    assert "openspec_finalize" in serialized and '"draft": true' in serialized
    assert "openspec_source_resolve" in serialized
    assert serialized.index('"taskReferenceName": "openspec_workspace"') < \
        serialized.index('"taskReferenceName": "model_policy"')


def test_openspec_simple_tasks_have_registered_definitions_and_new_ones_are_bounded():
    wf = _load("openspec_development")
    names = {node["name"] for node in _walk(wf) if node.get("type") == "SIMPLE"}
    defs = {json.loads(path.read_text())["name"]: json.loads(path.read_text())
            for path in (WF / "taskdefs").glob("*.json")}
    assert not (names - set(defs))
    for name in names:
        if name.startswith("openspec_"):
            assert defs[name]["pollTimeoutSeconds"] > 0
            assert defs[name]["responseTimeoutSeconds"] > 0
            assert defs[name]["timeoutSeconds"] > 0


def test_child_workflow_extensions_are_optional_and_forward_context():
    parallel = _load("code_parallel")
    campaign = _load("feature_campaign")
    assert parallel["inputTemplate"]["usePrecomputedPlan"] is False
    assert parallel["inputTemplate"]["specContextPath"] == ""
    assert campaign["inputTemplate"]["useImportedPlan"] is False
    assert campaign["inputTemplate"]["specContextPath"] == ""
    assert "specContextPath" in json.dumps(_load("code_subtask"))
    assert "specContextPath" in json.dumps(_load("campaign_subtask"))
