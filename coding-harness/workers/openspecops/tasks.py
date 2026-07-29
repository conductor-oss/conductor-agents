"""Discrete OpenSpec-CLI worker tasks that drive `openspec_plan.json`'s planning
sub-workflow: scaffold a change, read per-artifact generation instructions, read
change status, and deterministically parse the generated tasks.md into the
subtasks[] shape code_parallel's FORK_JOIN_DYNAMIC fan-out expects. These are
typed Conductor tasks (not agent-decided shell calls) so the sequencing itself
is inspectable/retryable/resumable the same way git_clone or worktree_add are.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from conductor.client.worker.worker_task import worker_task

from common import openspec_cli
from common.results import fail, ok
from common.tasks_md import TasksMdError, parse_tasks_md

_CHECK_OUTPUT_TAIL = 8000


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


@worker_task(task_definition_name="openspec_run_subtask_check")
def openspec_run_subtask_check(task):
    """Execute one subtask's declared `Test:` command for real against the merged
    worktree — the deterministic half of code_parallel's post-merge verification
    loop. Never raises on a failing command: a failing test — including one whose
    declared command can't even be launched (missing binary, not executable) — is
    an expected outcome the verification loop's fixup pass acts on, not a worker
    error (see common/results.py).

    The captured stdout/stderr is returned under the key ``log``, not ``output`` —
    the workflow's FORK_JOIN_DYNAMIC/JOIN consumers unwrap each entry via the
    codebase's established ``value.output // value`` pattern (see aggregate() in
    workflows/code_parallel.json), which exists because some join entries arrive
    pre-wrapped under an ``output`` key; a task-level field also named ``output``
    would collide with that unwrap and get mistaken for the whole result object."""
    i = task.input_data or {}
    repo_path = i.get("repoPath") or ""
    test_cmd = i.get("testCmd") or ""
    subtask_id = i.get("id") or ""
    try:
        if not repo_path:
            raise ValueError("repoPath is required")
        if not test_cmd.strip():
            return ok(task, {"id": subtask_id, "passed": True, "exitCode": 0, "log": ""},
                      [f"[openspec_run_subtask_check] {subtask_id}: no Test: command declared, skipping"])
        argv = shlex.split(test_cmd)
        try:
            proc = subprocess.run(argv, cwd=repo_path, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True)
        except OSError as exc:
            # The command itself couldn't be launched (missing binary, permission
            # denied, etc.) — a check failure for the loop to fix, not a crash.
            log = f"{type(exc).__name__}: {exc}"
            return ok(task, {"id": subtask_id, "passed": False, "exitCode": None, "log": log}, [
                f"[openspec_run_subtask_check] {subtask_id}: `{test_cmd}` -> FAILED to launch ({log})"
            ])
        log = ((proc.stdout or "") + (proc.stderr or ""))[-_CHECK_OUTPUT_TAIL:]
        passed = proc.returncode == 0
        return ok(task, {"id": subtask_id, "passed": passed, "exitCode": proc.returncode, "log": log}, [
            f"[openspec_run_subtask_check] {subtask_id}: `{test_cmd}` -> "
            f"{'passed' if passed else 'FAILED'} (exit {proc.returncode})"
        ])
    except Exception as e:  # noqa: BLE001
        return fail(task, "openspec_run_subtask_check", e)


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
        if not repo_path or not change_id:
            raise ValueError("repoPath and changeId are required")
        proc = subprocess.run(
            [openspec_cli.BIN, "validate", change_id, "--type", "change",
             "--strict", "--no-interactive", "--json"],
            cwd=repo_path, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}
        items = data.get("items") or []
        issues = (items[0].get("issues") if items else None) or []
        log = ((proc.stdout or "") + (proc.stderr or ""))[-_CHECK_OUTPUT_TAIL:]
        valid = proc.returncode == 0
        return ok(task, {"valid": valid, "exitCode": proc.returncode, "issues": issues, "log": log}, [
            f"[openspec_validate_change] {change_id} -> {'valid' if valid else 'INVALID'} "
            f"(exit {proc.returncode}, {len(issues)} issue(s))"
        ])
    except Exception as e:  # noqa: BLE001
        return fail(task, "openspec_validate_change", e)