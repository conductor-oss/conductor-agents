"""Model-policy context must survive every orchestration boundary."""

import json
from pathlib import Path

from campaign.tasks import campaign_schedule
from common.model_policy import select_role_tier


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / "workers" / "workflows"
ENVELOPE = {
    "modelProfile", "modelPolicy", "modelPolicySource", "modelPolicySha256",
    "modelsConfig", "modelOverrides",
}


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _load(path: Path):
    return json.loads(path.read_text())


def test_every_workflow_declares_and_preflights_the_model_policy_envelope():
    for path in sorted(WORKFLOWS.glob("*.json")):
        workflow = _load(path)
        assert ENVELOPE <= set(workflow["inputParameters"]), path.name
        policy_index = next(i for i, task in enumerate(workflow["tasks"])
                            if task.get("taskReferenceName") == "model_policy")
        assert workflow["tasks"][policy_index]["name"] == "model_profile_resolve", path.name
        if workflow["name"] == "openspec_development":
            workspace_index = next(i for i, task in enumerate(workflow["tasks"])
                                   if task.get("taskReferenceName") == "openspec_workspace")
            assert policy_index > workspace_index
        else:
            assert policy_index == 0


def test_every_coding_agent_receives_a_role_and_preflight_resolution():
    for path in sorted(WORKFLOWS.glob("*.json")):
        for task in _walk(_load(path)):
            if task.get("type") == "SIMPLE" and task.get("name") == "coding_agent":
                inputs = task["inputParameters"]
                assert inputs["modelRole"] in {"design", "plan", "code", "review", "judge"}
                assert inputs["modelResolution"] == "${model_policy.output}"
                assert ENVELOPE <= set(inputs)


def test_subworkflows_and_started_workflows_forward_the_envelope():
    for path in sorted(WORKFLOWS.glob("*.json")):
        for task in _walk(_load(path)):
            if task.get("type") == "SUB_WORKFLOW":
                assert ENVELOPE <= set(task.get("inputParameters", {})), path.name
            if task.get("type") == "START_WORKFLOW":
                child = task["inputParameters"]["startWorkflow"]
                if isinstance(child["version"], int):
                    assert child["version"] >= 1
                else:
                    assert child["version"] == "${workflow.input.childWorkflowVersion}"
                assert ENVELOPE <= set(child["input"]), path.name


def test_code_parallel_dynamic_fork_serializes_the_envelope():
    from common import code_parallel

    workflow = _load(WORKFLOWS / "code_parallel.json")
    builder = next(task for task in workflow["tasks"] if task["taskReferenceName"] == "build_forks")
    assert builder["name"] == "code_parallel_build_forks"
    assert ENVELOPE <= set(builder["inputParameters"])

    # code_parallel_build_forks used to be a jq expression; the envelope must
    # actually land on every dynamic-fork subtask input it constructs, not
    # merely appear among the task's own declared inputs.
    forks = code_parallel.build_forks(
        repo_path="/tmp", subtasks=[{"id": "a", "description": "d", "files": ["a.py"]}],
        change_dir="", code_model="", code_prompt_template="", code_prompt_template_source="",
        spec_context_path="", context_paths=[], max_turns=1, max_budget_usd=1,
        model_profile="openai-standard", model_policy={"version": 1}, model_policy_source="user:test",
        model_policy_sha256="deadbeef", models_config="", model_overrides={"x": 1})
    subtask_input = forks["dynamicTasksInput"]["a"]
    assert subtask_input["modelProfile"] == "openai-standard"
    assert subtask_input["modelPolicy"] == {"version": 1}
    assert subtask_input["modelPolicySource"] == "user:test"
    assert subtask_input["modelPolicySha256"] == "deadbeef"
    assert subtask_input["modelsConfig"] == ""
    assert subtask_input["modelOverrides"] == {"x": 1}


def test_code_parallel_children_keep_the_policy_code_tier_instead_of_a_legacy_model():
    """The dynamic-fork envelope reaches code_subtask and remains role-aware."""
    policy = {
        "version": 1,
        "defaultProfile": "openai-standard",
        "profiles": {"openai-standard": {"roles": {
            "code": {"tiers": [{"agent": "codex", "model": "gpt-5.6-terra"}]},
            "plan": {"agent": "codex", "model": "gpt-5.6-sol"},
        }}},
    }
    envelope = {"modelProfile": "openai-standard", "modelPolicy": policy,
                "modelPolicySource": "user:test", "modelsConfig": "",
                "modelOverrides": {}}
    workflow = _load(WORKFLOWS / "code_parallel.json")
    builder = next(task for task in workflow["tasks"] if task["taskReferenceName"] == "build_forks")
    assert ENVELOPE <= set(builder["inputParameters"])
    tier, _ = select_role_tier(envelope, role="code")
    assert tier["agent"] == "codex"
    assert tier["model"] == "gpt-5.6-terra"


def test_campaign_schedule_copies_envelope_to_each_dynamic_subtask(fake_task_input):
    plan = {"tasks": [{"id": "one", "description": "do it", "dependsOn": [],
                       "files": ["src/one.py"], "acceptanceCriteria": ["works"], "checks": ["unit"]}]}
    task = fake_task_input(repoPath="/tmp/repo", plan=plan, wave=1,
                           **{key: {"x": 1} if key in {"modelPolicy", "modelOverrides"} else "value"
                              for key in ENVELOPE})
    result = campaign_schedule(task)
    payload = result.output_data["dynamicTasksInput"]["wave_1_one"]
    assert ENVELOPE <= set(payload)
