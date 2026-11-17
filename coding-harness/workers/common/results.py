"""TaskResult + structured-log helpers.

Every worker returns a ``TaskResult`` so that successes carry a concise log line
and failures carry the FULL error (message + stdout/stderr) in the Conductor
``logs`` tab for debugging. Mirrors the TS ``workers/logs.ts`` contract.

Workers should not raise for *expected* outcomes (a failing test, a rejected
review) — those are encoded in ``output_data`` fields the workflow branches on.
Raise / return ``fail()`` only for genuine errors.
"""

from __future__ import annotations

from typing import Any

from conductor.client.http.models.task import Task
from conductor.client.http.models.task_result import TaskResult
from conductor.client.http.models.task_result_status import TaskResultStatus


def cap(s: Any, limit: int = 4000) -> str:
    """Truncate a string to ``limit`` chars, noting how much was dropped."""
    if s is None:
        return ""
    t = str(s).strip()
    if len(t) <= limit:
        return t
    return t[:limit] + f"\n…[truncated {len(t) - limit} chars]"


# One output field above this is a defect in that field's contract (a task output is a message, not a
# dump): a 1 MB list of rejected node_modules paths, copied by every join, pushed a team workflow past
# Conductor's 10 MB payload limit (run a87e9ddb). The field is named in the output and the task log so
# the next unbounded field is seen on its first run, not when a workflow dies of it.
FIELD_BYTES_LIMIT = 256 * 1024


def oversized_fields(output: dict[str, Any]) -> list[dict]:
    import json
    found = []
    for key, value in (output or {}).items():
        try:
            size = len(json.dumps(value, default=str))
        except (TypeError, ValueError):
            continue
        if size > FIELD_BYTES_LIMIT:
            found.append({"field": key, "bytes": size})
    return found


def ok(task: Task, output: dict[str, Any], logs: list[str] | None = None) -> TaskResult:
    """COMPLETED result with output data and optional log lines."""
    tr = task.to_task_result(TaskResultStatus.COMPLETED)
    oversized = oversized_fields(output)
    if oversized:
        import logging
        output = {**output, "oversizedFields": oversized}
        logging.getLogger("coding-harness.results").error(
            "task %s output fields over %d bytes: %s", getattr(task, "task_def_name", "?"),
            FIELD_BYTES_LIMIT, oversized)
        logs = [*(logs or []), f"[results] output fields over {FIELD_BYTES_LIMIT} bytes: {oversized}"]
    tr.output_data = output
    for line in logs or []:
        tr.log(line)
    return tr


def fail(task: Task, context: str, error: Any, logs: list[str] | None = None,
         output: dict[str, Any] | None = None) -> TaskResult:
    """FAILED result that captures the full error in the logs tab."""
    tr = task.to_task_result(TaskResultStatus.FAILED)
    msg = str(error)
    tr.reason_for_incompletion = f"{context}: {msg}"[:500]
    tr.output_data = output or {"error": msg[:500], "context": context}
    for line in logs or []:
        tr.log(line)
    tr.log(f"[{context}] ERROR: {cap(msg, 4000)}")
    # If the error carries captured process output, include it.
    for attr in ("stdout", "stderr"):
        val = getattr(error, attr, None)
        if val:
            tr.log(f"[{context}] {attr}: {cap(val, 2000)}")
    return tr
