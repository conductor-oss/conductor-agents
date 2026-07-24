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
    assert wf["version"] == 2 and wf["schemaVersion"] == 2
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


def test_every_new_simple_has_a_bounded_registered_definition():
    names = {x["name"] for wf_name in ("feature_campaign", "campaign_subtask")
             for x in _walk(_load(wf_name)) if x.get("type") == "SIMPLE"}
    defs = {json.loads(p.read_text())["name"]: json.loads(p.read_text())
            for p in (WF / "taskdefs").glob("*.json")}
    assert not (names - set(defs))
    for name in names:
        if name.startswith("campaign_"):
            assert defs[name]["pollTimeoutSeconds"] > 0
            assert defs[name]["responseTimeoutSeconds"] > 0
            assert defs[name]["timeoutSeconds"] > 0


def test_code_parallel_remains_independent_from_campaign():
    wf = _load("code_parallel")
    assert wf["name"] == "code_parallel" and wf["version"] == 3
    assert "feature_campaign" not in json.dumps(wf)
