"""Conductor task wrapper for model-policy resolution."""
from __future__ import annotations

from conductor.client.worker.worker_task import worker_task

from common.model_policy import ModelPolicyError, resolve_model_policy
from common.results import fail, ok


@worker_task(task_definition_name="model_profile_resolve")
def model_profile_resolve(task):
    try:
        data = task.input_data or {}
        worktree = data.get("worktreePath") or data.get("repoPath") or None
        result = resolve_model_policy(data, worktree=worktree)
        return ok(task, result, [f"[model_profile_resolve] profile={result['profile']} sha={result['canonicalSha256'][:12]}"])
    except ModelPolicyError as exc:
        return fail(task, "model_profile_resolve", exc)
    except Exception as exc:  # noqa: BLE001
        return fail(task, "model_profile_resolve", exc)
