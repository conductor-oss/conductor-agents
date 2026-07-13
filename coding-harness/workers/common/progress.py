"""Mid-execution progress reporting for long-running Conductor tasks.

A coding_agent task can run for minutes across many turns. Conductor only sees the
final TaskResult when the worker function returns, so without this the UI shows a
task stuck "IN_PROGRESS" with no detail. ``ProgressReporter`` pushes interim
IN_PROGRESS updates (status + current output_data) so users can watch turns arrive:

  * after every turn (the agent's ``on_turn`` callback → ``update``), and
  * at least every ``heartbeat_s`` seconds regardless — the background thread pushes
    the latest snapshot even when a single turn runs longer than that, so the task
    never looks frozen.

ALL HTTP pushes happen on the reporter's background thread: ``update()`` only
records the snapshot and wakes the thread. That keeps the per-turn path free of
blocking I/O, which matters because the async coding_agent worker calls it from
the AsyncTaskRunner's shared event loop — a blocking push there would stall every
concurrently running task.

Interim updates use ``TaskResourceApi.update_task`` with status IN_PROGRESS. When the
worker function later returns its terminal TaskResult, the poller's own update
overwrites this with COMPLETED/FAILED. Push failures are swallowed — a progress
update must never break the actual task.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from conductor.client.http.api.task_resource_api import TaskResourceApi
from conductor.client.http.api_client import ApiClient
from conductor.client.http.models.task_result_status import TaskResultStatus
from conductor.client.configuration.configuration import Configuration

log = logging.getLogger("coding_agent.progress")

# One shared API client for interim updates (thread-safe for our use: independent
# update_task calls). Built lazily from the same env Configuration the worker uses.
_API: TaskResourceApi | None = None


def _api() -> TaskResourceApi:
    global _API
    if _API is None:
        _API = TaskResourceApi(ApiClient(configuration=Configuration()))
    return _API


class ProgressReporter:
    """Pushes IN_PROGRESS snapshots for one running task."""

    def __init__(self, task: Any, heartbeat_s: float = 30.0) -> None:
        self._task = task
        self._heartbeat_s = heartbeat_s
        self._lock = threading.Lock()          # guards _latest / _seq
        self._push_lock = threading.Lock()     # serializes HTTP pushes, orders them
        self._latest: dict[str, Any] = {"status": "IN_PROGRESS", "turns": [], "numTurns": 0}
        self._seq = 0                           # bumped on every update(); monotonic
        self._pushed_seq = -1                   # highest seq actually sent
        self._started = time.monotonic()
        self._last_push = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()         # set by update() → immediate push
        self._thread: threading.Thread | None = None

    # --- public API ----------------------------------------------------------
    def start(self) -> "ProgressReporter":
        """Begin the heartbeat thread. Safe to call once."""
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True,
                                        name="coding_agent-progress")
        self._thread.start()
        return self

    def update(self, output: dict[str, Any]) -> None:
        """Record the latest partial output and wake the pusher thread (per-turn
        path). No I/O here — safe to call from an event loop or any thread."""
        with self._lock:
            self._seq += 1
            self._latest = output
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()  # unblock the wait so the thread exits promptly
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "ProgressReporter":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # --- internals -----------------------------------------------------------
    def _heartbeat_loop(self) -> None:
        # Two triggers, one thread: a per-turn update() sets _wake for an immediate
        # push; otherwise wake every 2s and push when >heartbeat_s has elapsed since
        # the last one, so a turn longer than the interval still emits liveness.
        while not self._stop.is_set():
            woken = self._wake.wait(2.0)
            if self._stop.is_set():
                break
            if woken:
                self._wake.clear()
                self._push()
            elif time.monotonic() - self._last_push >= self._heartbeat_s:
                self._push()

    def _push(self) -> None:
        # Serialize pushes and drop any that would move the snapshot backwards: the
        # per-turn (asyncio) and heartbeat (thread) paths can race, and an older
        # snapshot arriving last would make progress appear to jump back.
        with self._push_lock:
            with self._lock:
                seq = self._seq
                out = dict(self._latest)
            if seq < self._pushed_seq:
                return
            self._pushed_seq = seq
            self._last_push = time.monotonic()
            try:
                tr = self._task.to_task_result(TaskResultStatus.IN_PROGRESS)
                out.setdefault("status", "IN_PROGRESS")
                out["running"] = True
                out["elapsedSeconds"] = round(time.monotonic() - self._started, 1)
                tr.output_data = out
                _api().update_task(tr)
            except Exception as e:  # noqa: BLE001 — progress must never break the task
                log.debug("progress update failed (continuing): %s", e)
