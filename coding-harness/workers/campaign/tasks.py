"""Conductor worker entrypoints for feature_campaign."""

from __future__ import annotations

import json
import os
from pathlib import Path

from conductor.client.worker.worker_task import worker_task

from common import feature_campaign, git
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


def _mapping(value):
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return _mapping(parsed)
    return {}


def _reject_directory_write_roots(result: dict, repo_path: object) -> dict:
    """A plan's ``files`` contract names exact files, never broad directories."""
    root = Path(str(repo_path)).resolve() if str(repo_path or "").strip() else None
    if root is None or not result.get("valid"):
        return result
    errors = list(result.get("errors") or [])
    for task in result.get("tasks") or []:
        for path in task.get("files") or []:
            if (root / path).is_dir():
                errors.append(f"task {task['id']!r} files must name exact files, not directory {path!r}")
    if errors:
        result = {**result, "valid": False, "errors": errors}
    return result


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
        result = _reject_directory_write_roots(result, i.get("repoPath"))
        result["planLocation"] = ""
        if result["valid"] and i.get("repoPath"):
            rel = i.get("planPath") or ".conductor-code/campaign-plan.json"
            path = Path(i["repoPath"]) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"tasks": result["tasks"]}, indent=2) + "\n", encoding="utf-8")
            result["planLocation"] = str(rel)
        # The exact files this plan authorizes, so a workflow does not need a jq
        # task to flatten them before handing them to an agent as a write scope.
        result["allowedWriteRoots"] = sorted({
            path for entry in result.get("tasks") or []
            for path in (entry.get("files") or [])
            if isinstance(path, str) and path.strip()})
        return ok(task, result, [f"[campaign_validate_plan] valid={result['valid']} "
                                 f"tasks={len(result['tasks'])} "
                                 f"scope={len(result['allowedWriteRoots'])}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_validate_plan", exc)


@worker_task(task_definition_name="campaign_schedule")
def campaign_schedule(task):
    i = task.input_data or {}
    try:
        validated = validate_plan(i.get("plan"), max_tasks=int(i.get("maxTasks") or 25))
        validated = _reject_directory_write_roots(validated, i.get("repoPath"))
        if not validated["valid"]:
            # ``FORK_JOIN_DYNAMIC`` deserializes these fields before any later
            # checkpoint can report the invalid plan.  Always provide empty
            # collections on this fail-soft path; omitting them resolves to
            # null and crashes the workflow with a Conductor deserialization
            # error instead of preserving the validation evidence.
            return ok(task, {**validated, "ready": [], "readyIds": [],
                             "remainingIds": [], "blockedIds": [],
                             "unresolvedIds": [], "stalled": True,
                             "dynamicTasks": [], "dynamicTasksInput": {},
                             "wave": int(i.get("wave") or 1)})
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
                            "subWorkflowParam": {"name": "campaign_subtask", "version": 1}})
            inputs[ref] = {"repoPath": repo, "task": item, "wave": number,
                           "agent": i.get("agent") or "", "model": i.get("model") or "",
                           "maxTurns": int(i.get("maxTurns") or 500),
                           "maxBudgetUsd": float(i.get("maxBudgetUsd") or 50.0),
                           "resumeSessionId": (i.get("sessions") or {}).get(item["id"], ""),
                            "specContextPath": i.get("specContextPath") or "",
                           "contextPaths": i.get("contextPaths") or [],
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
        # ``aggregate_usage`` also reports a flat list of session IDs.  The
        # campaign state needs the task-ID-to-session-ID mapping above so a
        # later wave can resume the matching subtask.  Do not let the usage
        # summary overwrite that state field.
        usage.pop("sessions", None)
        output = {"integrated": not failed and not conflicts, "merged": merged,
                  "completedTaskIds": completed, "failedTaskIds": failed,
                  "conflicts": conflicts, "sessions": sessions, **usage}
        logs.append(f"[campaign_integrate] completed={completed} failed={failed} conflicts={len(conflicts)}")
        return ok(task, output, logs)
    except Exception as exc:  # noqa: BLE001
        # Integration is deliberately fail-soft: orchestration returns to the checkpoint.
        return ok(task, {"integrated": False, "merged": merged, "completedTaskIds": completed,
                         "failedTaskIds": failed, "conflicts": conflicts,
                         "sessions": sessions, "error": str(exc),
                         **{key: value for key, value in aggregate_usage(usage_records).items()
                            if key != "sessions"}},
                  logs + [f"[campaign_integrate] fail-soft error: {exc}"])


@worker_task(task_definition_name="campaign_merge_state")
def campaign_merge_state(task):
    """Merge wave progress with explicit, stable Python data semantics."""
    i = task.input_data or {}
    try:
        added = _list(i.get("added"))
        completed = list(dict.fromkeys([*_list(i.get("completed")), *added]))
        new_sessions = _mapping(i.get("newSessions"))
        if not new_sessions and isinstance(i.get("newSessions"), list):
            new_sessions = dict(zip(added, i["newSessions"], strict=False))
        sessions = _mapping(i.get("sessions"))
        sessions.update(new_sessions)
        remaining = [ident for ident in _list(i.get("remaining")) if ident not in completed]
        waves = list(dict.fromkeys([*_list(i.get("waves")), i.get("wave")]))
        waves = [wave for wave in waves if wave is not None]
        result = {"completed": completed, "sessions": sessions,
                  "remaining": remaining, "waves": waves}
        return ok(task, {"result": result},
                  [f"[campaign_merge_state] completed={len(completed)} remaining={len(remaining)}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_merge_state", exc)


# Retained as dormant reference code only. No workflow or registered task definition uses it.
# Do not restore the worker_task decorator unless campaign checks are deliberately reintroduced.
def campaign_checks(task):
    i = task.input_data or {}
    try:
        profile = str(i.get("profile") or "")
        if not profile:
            config = load_config(i["repoPath"], i.get("configPath") or ".conductor-code/checks.json")
            key = "finalProfile" if i.get("phase") == "final" else "waveProfile"
            profile = str((config.get("defaults") or {}).get(key) or "")
        if not profile:
            return ok(task, {"passed": True, "blockingPassed": True, "executionOutcome": "passed", "profile": "",
                             "checks": [], "skipped": True, "reason": "no profile configured"})
        output = run_profile(i["repoPath"], profile, requested=_list(i.get("checks")) or None,
                             config_path=i.get("configPath") or ".conductor-code/checks.json",
                             attached_confirmed=bool(i.get("attachedConfirmed", False)))
        return ok(task, output, [f"[campaign_checks] profile={profile} blockingPassed={output['blockingPassed']}"])
    except (ChecksConfigError, OSError, ValueError) as exc:
        # A configuration/check failure is a checkpoint result, not orchestration failure.
        return ok(task, {"passed": False, "blockingPassed": False, "executionOutcome": "configuration_blocked", "checks": [],
                         "profile": i.get("profile") or "", "error": str(exc)},
                  [f"[campaign_checks] fail-soft: {exc}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_checks", exc)


def _blocking_passed(inputs: dict) -> bool:
    """Decide whether the blocking phase signals allow this checkpoint to pass.

    An explicit ``blockingPassed`` still wins. Otherwise every supplied signal
    must be affirmative: a phase is only clear when its agent reported success
    and any accompanying validity or integration flag is true.
    """
    if inputs.get("blockingPassed") is not None:
        return _truth(inputs.get("blockingPassed"), True)
    signals = [key for key in ("blockingStatus", "blockingValid", "blockingIntegrated")
               if inputs.get(key) is not None]
    if not signals:
        return True
    if "blockingStatus" in signals and str(inputs["blockingStatus"]).strip() != "success":
        return False
    return all(_truth(inputs[key], False) for key in signals if key != "blockingStatus")


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
        # The workflow used to compute this in four separate one-line jq tasks
        # (`.status == "success"` and friends). The signals are inputs now and
        # the verdict is derived here, where it is unit-testable.
        result = validate_checkpoint(i.get("decision"), phase=str(i.get("phase") or ""),
                                     blocking_passed=_blocking_passed(i),
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
        uncommitted = sorted(git.status_files(repo))
        candidate = git.head(repo)
        return ok(task, {"outcome": i.get("outcome") or "incomplete", "branch": i.get("branch") or "",
                         "verifiedBranch": i.get("branch") if i.get("outcome") == "verified" else "",
                         "candidateCommit": candidate, "repoPath": repo,
                         "sourceRepoPath": i.get("sourceRepoPath") or repo,
                         "inPlace": _truth(i.get("inPlace"), False),
                         "changedFiles": changed, "uncommittedFiles": uncommitted,
                         **usage})
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_summary", exc)


@worker_task(task_definition_name="campaign_implementation_done")
def campaign_implementation_done(task):
    i = task.input_data or {}
    done = feature_campaign.is_implementation_done(outcome=i.get("outcome"), remaining=i.get("remaining"))
    return ok(task, {"done": done}, [f"[campaign_implementation_done] done={done}"])


@worker_task(task_definition_name="campaign_publication_plan")
def campaign_publication_plan(task):
    i = task.input_data or {}
    out = feature_campaign.resolve_campaign_publication_plan(
        outcome=i.get("outcome"), requested_draft=i.get("requestedDraft"),
        test_state=i.get("testState"), tests_passed=i.get("testsPassed"),
        tested=i.get("tested"), committed=i.get("committed"),
        agent_authored_test=i.get("agentAuthoredTest"))
    return ok(task, out, [f"[campaign_publication_plan] draft={out['draft']} outcome={out['outcome']}"])
