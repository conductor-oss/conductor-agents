"""Discrete OpenSpec-CLI worker tasks that drive `openspec_plan.json`'s planning
sub-workflow: scaffold a change, read per-artifact generation instructions, read
change status, and deterministically parse the generated tasks.md into the
subtasks[] shape code_parallel's FORK_JOIN_DYNAMIC fan-out expects. These are
typed Conductor tasks (not agent-decided shell calls) so the sequencing itself
is inspectable/retryable/resumable the same way git_clone or worktree_add are.
"""

from __future__ import annotations

from conductor.client.worker.worker_task import worker_task

from common import openspec_cli
from common.results import fail, ok
from common.tasks_md import TasksMdError, parse_tasks_md


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
        if not path:
            return fail(task, "openspec_tasks_to_subtasks", "tasksPath is required")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        subtasks = parse_tasks_md(text)
        return ok(task, {"subtasks": subtasks}, [
            f"[openspec_tasks_to_subtasks] {len(subtasks)} independent group(s): "
            f"{', '.join(s['id'] for s in subtasks)}"
        ])
    except (TasksMdError, OSError) as e:
        return fail(task, "openspec_tasks_to_subtasks", e)
