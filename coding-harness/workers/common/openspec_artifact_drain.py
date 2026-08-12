"""Pure decision logic for openspec_artifact_drain, replacing two JSON_JQ_TRANSFORM tasks."""

from __future__ import annotations


def _num(value: object) -> float | int:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def select_ready(*, status: object, generated: object, repo_path: object, change_name: object,
                 feedback: object, goal: object, model: object, max_turns: object,
                 max_budget_usd: object, model_profile: object, model_policy: object,
                 model_policy_source: object, model_policy_sha256: object,
                 models_config: object, model_overrides: object) -> dict:
    """Build one dynamic-fork SUB_WORKFLOW per artifact that is ready and not already generated."""
    already = set(_listed(generated))
    ready = [artifact for artifact in _listed(_mapping(status).get("artifacts"))
             if _mapping(artifact).get("status") == "ready"
             and _mapping(artifact).get("id") not in already]
    dynamic_tasks = []
    dynamic_inputs = {}
    ready_ids = []
    for artifact in ready:
        artifact_id = _mapping(artifact).get("id")
        ready_ids.append(artifact_id)
        ref = f"gen_{artifact_id}"
        dynamic_tasks.append({"name": "openspec_generate_artifact", "taskReferenceName": ref,
                              "type": "SUB_WORKFLOW",
                              "subWorkflowParam": {"name": "openspec_generate_artifact", "version": 1}})
        dynamic_inputs[ref] = {
            "repoPath": repo_path, "changeName": change_name, "artifact": artifact_id,
            "goal": goal, "feedback": feedback, "model": model, "maxTurns": max_turns,
            "maxBudgetUsd": max_budget_usd, "modelProfile": model_profile,
            "modelPolicy": model_policy, "modelPolicySource": model_policy_source,
            "modelPolicySha256": model_policy_sha256, "modelsConfig": models_config,
            "modelOverrides": model_overrides,
        }
    return {"dynamicTasks": dynamic_tasks, "dynamicTasksInput": dynamic_inputs,
            "readyIds": ready_ids, "readyCount": len(ready_ids)}


def merge_pass_progress(*, fan_output: object, ready_ids: object, prev_generated: object,
                        prev_files: object, prev_cost: object, prev_tokens: object) -> dict:
    """Fold one drain pass's dynamic-fork results into the running totals."""
    ready = _listed(ready_ids)
    values = [_mapping(entry).get("output") or _mapping(entry) or {}
             for entry in _mapping(fan_output).values()]
    values = [_mapping(value) for value in values]

    new_files: list = []
    if ready:
        for value in values:
            new_files.extend(_listed(value.get("filesChanged")))
    files: list = []
    for path in _listed(prev_files) + new_files:
        if path not in files:
            files.append(path)

    if ready:
        cost = sum(_num(value.get("costUsd")) for value in values) + _num(prev_cost)
        tokens = sum(_num(value.get("tokenUsed")) for value in values) + _num(prev_tokens)
    else:
        cost, tokens = _num(prev_cost), _num(prev_tokens)

    # jq's `unique` sorts as well as dedupes; match that exactly.
    generated = sorted(set(_listed(prev_generated)) | set(ready))
    return {"generated": generated, "filesChanged": files, "costUsd": cost, "tokenUsed": tokens}
