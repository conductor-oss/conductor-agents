"""Discrete OpenSpec-CLI worker tasks that drive `openspec_plan.json`'s planning
sub-workflow: scaffold a change, read per-artifact generation instructions, read
change status, and deterministically parse the generated tasks.md into the
subtasks[] shape code_parallel's FORK_JOIN_DYNAMIC fan-out expects. These are
typed Conductor tasks (not agent-decided shell calls) so the sequencing itself
is inspectable/retryable/resumable the same way git_clone or worktree_add are.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from conductor.client.worker.worker_task import worker_task

from common import (check_execution, openspec_artifact_drain, openspec_cli, openspec_development,
                    openspec_generate_artifact, plan_validation, polling, pr_description)
from common.results import fail, ok
from common.tasks_md import TasksMdError, parse_tasks_md

_CHECK_OUTPUT_TAIL = 8000


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _validated_subtasks(value: object, repo_path: object = "") -> list[dict]:
    """Compatibility wrapper around the single parallel-plan validator."""
    return plan_validation.validate_subtasks(value, repo_path)


@worker_task(task_definition_name="openspec_new_change")
def openspec_new_change(task):
    i = task.input_data or {}
    try:
        repo, name = i["repoPath"], i["name"]
        out = openspec_cli.new_change(repo, name, description=i.get("description") or None)
        seeded = openspec_cli.ensure_tasks_rule(repo)
        change = out["change"]
        return ok(task, {
            "changeName": change["id"],
            "changeDir": change["path"],
            "schema": change["schema"],
            "tasksRuleSeeded": seeded,
        }, [f"[openspec_new_change] {change['id']} @ {change['path']} tasksRuleSeeded={seeded}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "openspec_new_change", e)


@worker_task(task_definition_name="openspec_status")
def openspec_status(task):
    i = task.input_data or {}
    try:
        out = openspec_cli.status(i["repoPath"], i["changeName"])
        artifacts = out.get("artifacts") or []
        return ok(task, out, [
            f"[openspec_status] {i['changeName']} applyRequires={out.get('applyRequires')} "
            f"artifacts={[(a.get('id'), a.get('status')) for a in artifacts]}"
        ])
    except Exception as e:  # noqa: BLE001
        return fail(task, "openspec_status", e)


@worker_task(task_definition_name="openspec_instructions")
def openspec_instructions(task):
    i = task.input_data or {}
    try:
        out = openspec_cli.instructions(i["repoPath"], i["artifact"], i["changeName"])
        return ok(task, out, [
            f"[openspec_instructions] {i['artifact']} -> {out.get('resolvedOutputPath')} "
            f"rules={len(out.get('rules') or [])}"
        ])
    except Exception as e:  # noqa: BLE001
        return fail(task, "openspec_instructions", e)


@worker_task(task_definition_name="openspec_tasks_to_subtasks")
def openspec_tasks_to_subtasks(task):
    i = task.input_data or {}
    path = i.get("tasksPath") or ""
    try:
        if "useOpenSpec" in i and not _bool(i.get("useOpenSpec"), False):
            fallback = _validated_subtasks(i.get("fallbackSubtasks") or [], i.get("repoPath"))
            return ok(task, {"subtasks": fallback, "reparsed": False}, [
                f"[openspec_tasks_to_subtasks] generic document plan: {len(fallback)} group(s)"
            ])
        if not path:
            return fail(task, "openspec_tasks_to_subtasks", "tasksPath is required")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        subtasks = _validated_subtasks(parse_tasks_md(text), i.get("repoPath"))
        return ok(task, {"subtasks": subtasks, "reparsed": True}, [
            f"[openspec_tasks_to_subtasks] {len(subtasks)} independent group(s): "
            f"{', '.join(s['id'] for s in subtasks)}"
        ])
    except (TasksMdError, OSError, ValueError) as e:
        return fail(task, "openspec_tasks_to_subtasks", e)


@worker_task(task_definition_name="openspec_read_proposal")
def openspec_read_proposal(task):
    """Read an OpenSpec change's proposal.md content verbatim, for embedding in
    the plan_review human-approval draft and in composed PR bodies."""
    i = task.input_data or {}
    change_dir = i.get("changeDir") or ""
    try:
        if not change_dir:
            raise ValueError("changeDir is required")
        path = Path(change_dir) / "proposal.md"
        if not path.is_file():
            raise ValueError(f"proposal.md not found under {change_dir}")
        text = path.read_text(encoding="utf-8")
        return ok(task, {"proposalText": text}, [
            f"[openspec_read_proposal] {path} -> {len(text)} chars"
        ])
    except Exception as e:  # noqa: BLE001
        return fail(task, "openspec_read_proposal", e)


@worker_task(task_definition_name="openspec_validate_change")
def openspec_validate_change(task):
    """Run `openspec validate <id> --strict` for real without raising on
    failure — the deterministic spec-validity check the verification loop folds
    into its pass/fail gate, so a broken proposal/design/specs/tasks.md gets
    fixed by the loop's fixup pass instead of crashing later when `archive_change`
    (openspec_finalize) runs the same validate immediately before archiving."""
    i = task.input_data or {}
    repo_path = i.get("repoPath") or ""
    change_id = i.get("changeId") or ""
    try:
        if "skip" in i and str(i.get("skip") or "").strip().lower() not in {"true", "1", "yes", "on"}:
            return ok(task, {"valid": True, "skipped": True, "exitCode": 0, "issues": [], "log": ""}, [
                "[openspec_validate_change] skipped: generic document plan"
            ])
        if not repo_path or not change_id:
            raise ValueError("repoPath and changeId are required")
        argv = [openspec_cli.BIN, "validate", change_id, "--type", "change",
                "--strict", "--no-interactive", "--json"]
        with tempfile.TemporaryDirectory(prefix="conductor-openspec-validate-") as state_dir:
            env = check_execution.isolated_environment(state_dir)
            proc = check_execution.execute(argv, cwd=repo_path, env=env)
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}
        items = data.get("items") or []
        issues = (items[0].get("issues") if items else None) or []
        log = ((proc.stdout or "") + (proc.stderr or ""))[-_CHECK_OUTPUT_TAIL:]
        valid = proc.exit_code == 0
        return ok(task, {"valid": valid, "exitCode": proc.exit_code, "issues": issues, "log": log}, [
            f"[openspec_validate_change] {change_id} -> {'valid' if valid else 'INVALID'} "
            f"(exit {proc.exit_code}, {len(issues)} issue(s))"
        ])
    except Exception as e:  # noqa: BLE001
        return fail(task, "openspec_validate_change", e)


@worker_task(task_definition_name="execution_runtime_health")
def execution_runtime_health(task):
    """Live proof that the broad execution worker has every supported runtime."""
    report = check_execution.runtime_health_report()
    worker_guards = polling.registered_worker_guard_report()
    description_policy = pr_description.policy_health_report()
    report["workerGuards"] = worker_guards
    report["runtimeHealthy"] = bool(
        report["healthy"] and report["idleSleepInhibitionActive"] and worker_guards["healthy"]
    )
    report["prDescriptionPolicy"] = description_policy
    report["healthy"] = bool(report["runtimeHealthy"] and description_policy["healthy"])
    return ok(task, {**report, "workerRole": "execution", "modules": ["openspecops"]},
              [f"[execution_runtime_health] healthy={report['healthy']}"])


@worker_task(task_definition_name="runtime_health_summary")
def runtime_health_summary(task):
    """Combine both worker pools' health into the one answer AGENTS.md asks for.

    A command-launch change is only proven when the execution pool and the
    isolated verification pool are both healthy, so the conjunction is the
    result that matters and is named here rather than derived in jq.
    """
    i = task.input_data or {}
    execution = i.get("execution") if isinstance(i.get("execution"), dict) else {}
    verification = i.get("verification") if isinstance(i.get("verification"), dict) else {}
    execution_ok = execution.get("healthy") is True
    verification_ok = verification.get("healthy") is True
    return ok(task, {"healthy": execution_ok and verification_ok,
                     "executionHealthy": execution_ok,
                     "verificationHealthy": verification_ok},
              [f"[runtime_health_summary] execution={execution_ok} verification={verification_ok}"])


@worker_task(task_definition_name="openspec_select_ready")
def openspec_select_ready(task):
    i = task.input_data or {}
    out = openspec_artifact_drain.select_ready(
        status=i.get("status"), generated=i.get("generated"), repo_path=i.get("repoPath"),
        change_name=i.get("changeName"), feedback=i.get("feedback"), goal=i.get("goal"),
        model=i.get("model"), max_turns=i.get("maxTurns"), max_budget_usd=i.get("maxBudgetUsd"),
        model_profile=i.get("modelProfile"), model_policy=i.get("modelPolicy"),
        model_policy_source=i.get("modelPolicySource"), model_policy_sha256=i.get("modelPolicySha256"),
        models_config=i.get("modelsConfig"), model_overrides=i.get("modelOverrides"))
    return ok(task, out, [f"[openspec_select_ready] ready={out['readyCount']}"])


@worker_task(task_definition_name="openspec_merge_pass_progress")
def openspec_merge_pass_progress(task):
    i = task.input_data or {}
    out = openspec_artifact_drain.merge_pass_progress(
        fan_output=i.get("fanOutput"), ready_ids=i.get("readyIds"),
        prev_generated=i.get("prevGenerated"), prev_files=i.get("prevFiles"),
        prev_cost=i.get("prevCost"), prev_tokens=i.get("prevTokens"))
    return ok(task, out, [f"[openspec_merge_pass_progress] generated={len(out['generated'])}"])


@worker_task(task_definition_name="openspec_usage")
def openspec_usage(task):
    i = task.input_data or {}
    out = openspec_development.aggregate_usage(
        assessment=i.get("assessment"), child=i.get("child"), verification=i.get("verification"))
    return ok(task, out, [f"[openspec_usage] totalCostUsd={out['totalCostUsd']}"])


@worker_task(task_definition_name="openspec_build_prompt")
def openspec_build_prompt(task):
    i = task.input_data or {}
    prompt = openspec_generate_artifact.build_prompt(
        instr=i.get("instr"), goal=i.get("goal"), feedback=i.get("feedback"))
    return ok(task, {"prompt": prompt}, ["[openspec_build_prompt] composed"])
