"""Harness-level polling defaults for the Conductor Python SDK.

The pinned SDK currently sends ``timeout=100`` for every batch poll regardless
of the worker's ``poll_timeout`` setting.  Override that transport argument in
one place so a task type still has one poller, while idle queues use long polls.

The same patch bounds how long the poller waits for the poll *response*. The
server answers a batch poll within its ``timeout`` (a task arrived, or the long
poll expired), so a response that has not arrived well after that is a lost
one, not a slow one. conductor-python 1.3.11 passes ``timeout=None`` to httpx
for every request, which disables the client's 120 s default rather than
applying it; the ``coding_agent`` poller (pid 60595, 2026-11-07) sat in
``recv`` on a never-answered poll for over an hour with its queue holding a
SCHEDULED task, and nothing in the fleet noticed. A poll is a queue-polling
interval, not an execution deadline: when the bound trips the SDK logs the
failure, backs off and polls again; no running task is touched.
"""

from __future__ import annotations

import time
from functools import partial, update_wrapper
import os


def _execute_with_sleep_inhibition(function, task_name: str, task):
    from common import check_execution

    with check_execution.prevent_idle_system_sleep(f"Conductor task {task_name}"):
        return function(task)


def _sleep_inhibited(function, task_name: str):
    if getattr(function, "_conductor_harness_sleep_inhibited", False):
        return function
    wrapped = partial(_execute_with_sleep_inhibition, function, task_name)
    update_wrapper(wrapped, function)
    wrapped._conductor_harness_sleep_inhibited = True
    return wrapped


def poll_timeout_ms() -> int:
    try:
        return max(1_000, min(30_000, int(os.environ.get("CONDUCTOR_POLL_TIMEOUT_MS", "5000"))))
    except ValueError:
        return 5_000


def poll_interval_ms() -> int:
    try:
        return max(100, min(5_000, int(os.environ.get("CONDUCTOR_POLL_INTERVAL_MS", "500"))))
    except ValueError:
        return 500


POLL_RESPONSE_GRACE_S = 30.0
POLL_CONNECT_BOUND_S = 10.0


def poll_response_bound() -> tuple[float, float]:
    """``(connect, read)`` seconds the poller waits for one batch-poll response.

    The read bound is the server's own long-poll timeout plus a grace period,
    so a healthy long poll can never trip it; only a response that is not
    coming does. The value is a ``_request_timeout`` for the poll call alone.
    """
    return (POLL_CONNECT_BOUND_S, poll_timeout_ms() / 1000.0 + POLL_RESPONSE_GRACE_S)


# No task type runs single-file: a program's ranks fork several teams whose board steps (standups,
# integrations, commits, folds) arrive together, and one thread per type queued them behind each other.
MIN_WORKER_THREADS = 4


def worker_thread_count(task_name: str, current: int) -> int:
    """Resolve an operator concurrency floor for one task type.

    A concurrency bound is not an execution deadline: it decides how many tasks
    may run at once, never how long a running task may take. It only ever
    *raises* the value a worker declared (never below ``MIN_WORKER_THREADS``), so an
    explicit decorator setting made for a heavy task can never be silently lowered by a
    broad env var.
    """
    current = max(int(current or 1), MIN_WORKER_THREADS)
    specific = os.environ.get(f"CONDUCTOR_WORKER_THREADS_{task_name.upper()}")
    general = os.environ.get("CONDUCTOR_WORKER_THREADS")
    for value in (specific, general):
        if not value:
            continue
        try:
            requested = max(1, min(16, int(value)))
        except ValueError:
            continue
        return max(current, requested)
    return current


def _draining() -> bool:
    from common import source_watch
    return source_watch.draining()


def configure_sdk_polling() -> None:
    """Patch the SDK request boundary before worker processes are spawned."""
    from conductor.client.http.api.async_task_resource_api import AsyncTaskResourceApi
    from conductor.client.http.api.task_resource_api import TaskResourceApi

    if not getattr(TaskResourceApi, "_conductor_harness_poll_patch", False):
        original_sync = TaskResourceApi.batch_poll

        def batch_poll(self, tasktype, **kwargs):
            if _draining():
                # A reload is pending: claim nothing, let in-flight work finish (common.source_watch).
                time.sleep(poll_interval_ms() / 1000.0)
                return []
            kwargs["timeout"] = poll_timeout_ms()
            kwargs["_request_timeout"] = poll_response_bound()
            return original_sync(self, tasktype, **kwargs)

        TaskResourceApi.batch_poll = batch_poll
        TaskResourceApi._conductor_harness_poll_patch = True

    if not getattr(AsyncTaskResourceApi, "_conductor_harness_poll_patch", False):
        original_async = AsyncTaskResourceApi.batch_poll

        async def batch_poll(self, tasktype, **kwargs):
            if _draining():
                import asyncio
                await asyncio.sleep(poll_interval_ms() / 1000.0)
                return []
            kwargs["timeout"] = poll_timeout_ms()
            kwargs["_request_timeout"] = poll_response_bound()
            return await original_async(self, tasktype, **kwargs)

        AsyncTaskResourceApi.batch_poll = batch_poll
        AsyncTaskResourceApi._conductor_harness_poll_patch = True


def configure_registered_workers() -> None:
    """Set non-aborting poll behavior and mandatory in-flight lease renewal."""
    from conductor.client.automator.task_handler import _decorated_functions

    for (task_name, _domain), record in _decorated_functions.items():
        if record.get("poll_timeout", 100) == 100:
            record["poll_timeout"] = poll_timeout_ms()
        if record.get("poll_interval", 100) == 100:
            record["poll_interval"] = poll_interval_ms()
        # OSS Conductor normalizes responseTimeoutSeconds=0 to its server
        # default. Continuous lease renewal prevents that internal lease from
        # becoming a wall-clock deadline for healthy running work.
        record["lease_extend_enabled"] = True
        record["thread_count"] = worker_thread_count(task_name, int(record.get("thread_count") or 1))
        # Lease renewal cannot run while a laptop worker is suspended. Hold a
        # host power assertion only while this worker function is active.
        record["func"] = _sleep_inhibited(record["func"], task_name)


def registered_worker_guard_report() -> dict:
    """Inventory the guards actually installed on every worker in this process."""
    from conductor.client.automator.task_handler import _decorated_functions

    workers = []
    for (task_name, domain), record in sorted(
            _decorated_functions.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        lease = record.get("lease_extend_enabled") is True
        sleep = bool(getattr(record.get("func"), "_conductor_harness_sleep_inhibited", False))
        workers.append({
            "task": task_name,
            "domain": domain or "",
            "leaseRenewal": lease,
            "idleSleepInhibition": sleep,
            # Reported so a deployed fleet's real concurrency is provable from
            # verification_health output, not merely inferred from source.
            "threadCount": int(record.get("thread_count") or 1),
            "guarded": lease and sleep,
        })
    guarded = sum(1 for worker in workers if worker["guarded"])
    return {
        "healthy": bool(workers) and guarded == len(workers),
        "registeredWorkers": len(workers),
        "guardedWorkers": guarded,
        "workers": workers,
    }
