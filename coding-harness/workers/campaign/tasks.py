"""Conductor worker entrypoints for feature_campaign."""

from __future__ import annotations

import json
import os
from pathlib import Path

from conductor.client.worker.worker_task import worker_task

from common import git
from common.results import fail, ok
from .checks import ChecksConfigError, load_config, run_profile
from .model import aggregate_usage, select_wave, validate_checkpoint, validate_plan


def _list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [x.strip() for x in value.split(",") if x.strip()]
        except ValueError:
            return [x.strip() for x in value.split(",") if x.strip()]
    return []


def _truth(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "success", "completed")


@worker_task(task_definition_name="campaign_validate_plan")
def campaign_validate_plan(task):
    i = task.input_data or {}
    try:
        result = validate_plan(i.get("plan"), max_tasks=int(i.get("maxTasks") or 25))
        result["planLocation"] = ""
        if result["valid"] and i.get("repoPath"):
            rel = i.get("planPath") or ".conductor-code/campaign-plan.json"
            path = Path(i["repoPath"]) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"tasks": result["tasks"]}, indent=2) + "\n", encoding="utf-8")
            result["planLocation"] = str(rel)
        return ok(task, result, [f"[campaign_validate_plan] valid={result['valid']} tasks={len(result['tasks'])}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_validate_plan", exc)


@worker_task(task_definition_name="campaign_schedule")
def campaign_schedule(task):
    i = task.input_data or {}
    try:
        validated = validate_plan(i.get("plan"), max_tasks=int(i.get("maxTasks") or 25))
        if not validated["valid"]:
            return ok(task, {**validated, "ready": [], "readyIds": [], "stalled": True})
        wave = select_wave(validated["tasks"], _list(i.get("completedTaskIds")),
                           _list(i.get("blockedTaskIds")),
                           max_parallelism=int(i.get("maxParallelism") or 6))
        repo = i.get("repoPath")
        number = int(i.get("wave") or 1)
        dynamic, inputs = [], {}
        for item in wave["ready"]:
            ref = f"wave_{number}_{item['id']}"
            dynamic.append({"name": "campaign_subtask", "taskReferenceName": ref,
                            "type": "SUB_WORKFLOW",
                            "subWorkflowParam": {"name": "campaign_subtask", "version": 2}})
            inputs[ref] = {"repoPath": repo, "task": item, "wave": number,
                           "agent": i.get("agent") or "", "model": i.get("model") or "",
                           "maxTurns": int(i.get("maxTurns") or 500),
                           "maxBudgetUsd": float(i.get("maxBudgetUsd") or 50.0),
                           "resumeSessionId": (i.get("sessions") or {}).get(item["id"], ""),
                           "specContextPath": i.get("specContextPath") or "",
                           "codePromptTemplate": i.get("codePromptTemplate") or "",
                           "codePromptTemplateSource": i.get("codePromptTemplateSource") or "",
                           "feedback": i.get("feedback") or "",
                           "modelProfile": i.get("modelProfile") or "",
                           "modelPolicy": i.get("modelPolicy") or {},
                           "modelPolicySource": i.get("modelPolicySource") or "",
                           "modelPolicySha256": i.get("modelPolicySha256") or "",
                           "modelsConfig": i.get("modelsConfig") or "",
                           "modelOverrides": i.get("modelOverrides") or {}}
        output = {**wave, "valid": True, "errors": [], "dynamicTasks": dynamic,
                  "dynamicTasksInput": inputs, "wave": number}
        return ok(task, output, [f"[campaign_schedule] wave={number} ready={wave['readyIds']} stalled={wave['stalled']}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_schedule", exc)


@worker_task(task_definition_name="campaign_integrate")
def campaign_integrate(task):
    """Merge successful campaign branches, returning conflicts/failures as data."""
    i = task.input_data or {}
    repo = i.get("repoPath") or ""
    results = i.get("results") or {}
    if isinstance(results, list):
        results = {str(pos): value for pos, value in enumerate(results)}
    merged, completed, failed, conflicts, sessions = [], [], [], [], {}
    usage_records = []
    logs = []
    try:
        for _, wrapper in (results.items() if isinstance(results, dict) else []):
            out = wrapper.get("output", wrapper) if isinstance(wrapper, dict) else {}
            ident = str(out.get("taskId") or "")
            if out.get("sessionId"):
                sessions[ident] = out["sessionId"]
            usage_records.append(out)
            if out.get("status") not in ("success", "success_no_changes"):
                if ident:
                    failed.append(ident)
                continue
            branch = out.get("branch")
            try:
                git.git(repo, "merge", "--no-edit", branch)
                merged.append(branch)
                completed.append(ident)
                worktree = out.get("worktreePath")
                if worktree:
                    git.worktree_remove(repo, os.path.basename(str(worktree)))
            except Exception:  # noqa: BLE001
                paths = git.has_conflicts(repo)
                git.git(repo, "merge", "--abort", check=False)
                conflicts.append({"taskId": ident, "branch": branch, "files": paths})
                failed.append(ident)
        usage = aggregate_usage(usage_records)
        output = {"integrated": not failed and not conflicts, "merged": merged,
                  "completedTaskIds": completed, "failedTaskIds": failed,
                  "conflicts": conflicts, "sessions": sessions, **usage}
        logs.append(f"[campaign_integrate] completed={completed} failed={failed} conflicts={len(conflicts)}")
        return ok(task, output, logs)
    except Exception as exc:  # noqa: BLE001
        # Integration is deliberately fail-soft: orchestration returns to the checkpoint.
        return ok(task, {"integrated": False, "merged": merged, "completedTaskIds": completed,
                         "failedTaskIds": failed, "conflicts": conflicts,
                         "sessions": sessions, "error": str(exc), **aggregate_usage(usage_records)},
                  logs + [f"[campaign_integrate] fail-soft error: {exc}"])


@worker_task(task_definition_name="campaign_checks")
def campaign_checks(task):
    i = task.input_data or {}
    try:
        profile = str(i.get("profile") or "")
        if not profile:
            config = load_config(i["repoPath"], i.get("configPath") or ".conductor-code/checks.json")
            key = "finalProfile" if i.get("phase") == "final" else "waveProfile"
            profile = str((config.get("defaults") or {}).get(key) or "")
        if not profile:
            return ok(task, {"passed": True, "blockingPassed": True, "profile": "",
                             "checks": [], "skipped": True, "reason": "no profile configured"})
        output = run_profile(i["repoPath"], profile, requested=_list(i.get("checks")) or None,
                             config_path=i.get("configPath") or ".conductor-code/checks.json",
                             attached_confirmed=bool(i.get("attachedConfirmed", False)))
        return ok(task, output, [f"[campaign_checks] profile={profile} blockingPassed={output['blockingPassed']}"])
    except (ChecksConfigError, OSError, ValueError) as exc:
        # A configuration/check failure is a checkpoint result, not orchestration failure.
        return ok(task, {"passed": False, "blockingPassed": False, "checks": [],
                         "profile": i.get("profile") or "", "error": str(exc)},
                  [f"[campaign_checks] fail-soft: {exc}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_checks", exc)


@worker_task(task_definition_name="campaign_checkpoint")
def campaign_checkpoint(task):
    i = task.input_data or {}
    try:
        profile = str((i.get("decision") or {}).get("profile") or i.get("profile") or "")
        allowed = []
        if profile and i.get("repoPath"):
            try:
                config = load_config(i["repoPath"], i.get("configPath") or ".conductor-code/checks.json")
                allowed = list((config.get("profiles", {}).get(profile) or {}).get("checks") or [])
            except ChecksConfigError:
                pass
        result = validate_checkpoint(i.get("decision"), phase=str(i.get("phase") or ""),
                                     blocking_passed=_truth(i.get("blockingPassed"), True),
                                     allowed_checks=allowed)
        try:
            result["maxTurns"] = int(result.get("maxTurns") or i.get("maxTurns") or 500)
        except (TypeError, ValueError):
            result["maxTurns"] = int(i.get("maxTurns") or 500)
        try:
            result["maxBudgetUsd"] = float(result.get("maxBudgetUsd") or i.get("maxBudgetUsd") or 50.0)
        except (TypeError, ValueError):
            result["maxBudgetUsd"] = float(i.get("maxBudgetUsd") or 50.0)
        result["profiles"] = i.get("profiles") or {}
        if result["action"] == "set_profiles" and isinstance((i.get("decision") or {}).get("profiles"), dict):
            result["profiles"] = dict(result["profiles"])
            result["profiles"].update({k: v for k, v in (i.get("decision") or {})["profiles"].items()
                                       if v not in (None, "")})
        return ok(task, result, [f"[campaign_checkpoint] phase={result['phase']} action={result['action']} valid={result['valid']}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_checkpoint", exc)


def _usage_records(value):
    records = []
    if isinstance(value, dict):
        if any(k in value for k in ("tokenUsed", "totalTokens", "costUsd", "totalCostUsd")):
            records.append(value)
        for child in value.values():
            records.extend(_usage_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(_usage_records(child))
    return records


@worker_task(task_definition_name="campaign_summary")
def campaign_summary(task):
    i = task.input_data or {}
    try:
        usage = aggregate_usage(_usage_records(i.get("usage")))
        repo = i.get("repoPath") or ""
        base = str(i.get("baseCommit") or "HEAD~1")
        committed = git.git(repo, "diff", "--name-only", f"{base}..HEAD", check=False).stdout.splitlines()
        changed = sorted(set(committed) | set(git.status_files(repo)))
        sessions = i.get("sessions") or {}
        if isinstance(sessions, dict):
            usage["sessions"] = sessions
        return ok(task, {"outcome": i.get("outcome") or "incomplete", "branch": i.get("branch") or "",
                         "verifiedBranch": i.get("branch") if i.get("outcome") == "verified" else "",
                         "changedFiles": changed, **usage})
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_summary", exc)
