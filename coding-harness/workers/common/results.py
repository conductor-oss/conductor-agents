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


def ok(task: Task, output: dict[str, Any], logs: list[str] | None = None) -> TaskResult:
    """COMPLETED result with output data and optional log lines."""
    tr = task.to_task_result(TaskResultStatus.COMPLETED)
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
