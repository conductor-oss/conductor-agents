"""Coalesce Conductor's occasional same-task redelivery for non-idempotent work.

Conductor can redeliver a still-SCHEDULED task before a worker's final result is
posted -- confirmed live: two concurrent executions of
test_authored_test_commit_verify both ran `git commit` against the same repo for
the same task_id; the second saw nothing left to stage (the first had already
committed it) and its "no committable change" result overwrote the first's real
"accepted" result. coding_agent/tasks.py already guards its own worker the same
way this module generalizes: any worker task whose body is expensive and NOT
safe to run twice against the same checkout (a git commit, a real command
execution, an LLM session) should coalesce concurrent deliveries of the same
task_id through one ``TaskFlight`` instance, rather than running the work twice
and racing to report a result.
"""

from __future__ import annotations

import collections
import threading
from typing import Any, Callable

from conductor.client.http.models.task import Task
from conductor.client.http.models.task_result import TaskResult

from .results import fail

# A distinct sentinel, never a real cached value: `None` must remain usable as
# an actual result (a work() callable returning None outright is itself the
# "duplicate delivery ended without a primary result" case run() reports), so
# "no cached result yet" cannot be spelled as None without colliding with it.
_MISSING = object()


class TaskFlight:
    """One dedup registry -- construct one module-level instance per worker task."""

    def __init__(self, cache_limit: int = 256) -> None:
        self._lock = threading.Lock()
        self._flights: dict[str, threading.Event] = {}
        self._results: collections.OrderedDict[str, Any] = collections.OrderedDict()
        self._cache_limit = cache_limit

    def claim(self, task_id: str) -> tuple[bool, threading.Event, Any]:
        """Claim the sole execution for task_id, or join its in-flight/cached result.

        Returns (owner, event, cached_result). cached_result is _MISSING when
        there is nothing cached yet (join the returned event instead). The
        caller that gets owner=True must eventually call finish(); every other
        caller for the same task_id should wait() on the returned event, then
        read result(task_id).
        """
        with self._lock:
            if task_id in self._results:
                return False, threading.Event(), self._results[task_id]
            event = self._flights.get(task_id)
            if event is not None:
                return False, event, _MISSING
            event = threading.Event()
            self._flights[task_id] = event
            return True, event, _MISSING

    def finish(self, task_id: str, event: threading.Event, result: Any) -> None:
        with self._lock:
            self._flights.pop(task_id, None)
            self._results[task_id] = result
            self._results.move_to_end(task_id)
            while len(self._results) > self._cache_limit:
                self._results.popitem(last=False)
            event.set()

    def result(self, task_id: str) -> Any:
        with self._lock:
            return self._results.get(task_id, _MISSING)

    def run(self, task: Task, work: Callable[[], TaskResult]) -> TaskResult:
        """Run ``work()`` exactly once per task.task_id; a concurrent or later
        redelivery joins the primary's result instead of repeating the work."""
        task_id = str(task.task_id)
        owner, event, cached = self.claim(task_id)
        if cached is not _MISSING:
            return cached
        if not owner:
            event.wait()
            result = self.result(task_id)
            if result is not _MISSING:
                return result
            return fail(task, str(task.task_def_name), "duplicate delivery ended without a primary result")
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001
            result = fail(task, str(task.task_def_name), exc)
        self.finish(task_id, event, result)
        return result
