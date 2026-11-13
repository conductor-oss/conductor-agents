"""Hand back tasks a previous worker generation was running when it ended.

A generation restarts when its source changes, and ``source_watch`` only waits for work it can see as
a child process. A deterministic step runs inside the poller, so a restart can end it mid-flight; the
server then holds the task IN_PROGRESS with no worker until its response lease expires. A one-second
``test_plan_resolve`` claimed just before a reload stalled a whole program that way (run 373cac75).

Each generation therefore requeues what an earlier one left, which also covers a crash. Every
generation polls under its own identity, ``<host>/<start ms>/<leader pid>``, so a task is left behind
when its worker is another generation of this host whose leader process is gone, for a task type this
generation polls. A generation that is still draining beside this one keeps its tasks: they finish
there (requeued, they would run twice). The sweep repeats every ``SWEEP_SECONDS`` for the life of the
generation, not once at its start: a predecessor can claim a task after its successor started (a poll
already in flight when it began draining) and end before finishing it — the program's final test cycle
waited 13 minutes on a `model_profile_resolve` claimed 75 ms before its generation reloaded (run
884f7eec). A task claimed under an identity without a pid (a worker older than this rule) is judged
by its start time instead. The requeue reports IN_PROGRESS with no callback delay, so it uses none of
the task's retries.

The sweep reads every running workflow with its tasks: Conductor's task search cannot filter on the
worker.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from typing import Iterable

from conductor.client.http.api_client import ApiClient

from .conductor_config import configuration_from_env

log = logging.getLogger("coding-harness.workers.orphans")
_PAGE = 100


SWEEP_SECONDS = 30.0


def generation_identity(host: str, started_ms: int, pid: int | None = None) -> str:
    return f"{host}/{int(started_ms)}" + (f"/{int(pid)}" if pid else "")


def _pid_alive(pid: int) -> bool:
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def orphaned(workflow: dict, task_names: Iterable[str], *, identity: str, before_ms: int,
             alive=_pid_alive) -> list[dict]:
    """The tasks of one workflow this generation should requeue (pure, given ``alive``)."""
    names, host = set(task_names), identity.split("/", 1)[0]

    def left_behind(task: dict) -> bool:
        worker = str(task.get("workerId") or "")
        if worker == identity:
            return False
        if worker.startswith(host + "/"):
            parts = worker.split("/")
            if len(parts) >= 3 and parts[2].isdigit():
                return not alive(int(parts[2]))  # another generation of this host: gone, or still draining
            return 0 < int(task.get("startTime") or 0) < before_ms
        return worker == host and 0 < int(task.get("startTime") or 0) < before_ms

    return [t for t in workflow.get("tasks") or []
            if t.get("status") == "IN_PROGRESS" and t.get("taskDefName") in names and left_behind(t)]


def _session() -> tuple[str, dict]:
    """The server and the authentication headers, resolved once per sweep."""
    config = configuration_from_env()
    auth = ApiClient(configuration=config).get_authentication_headers() or {}
    return config.host, {"Content-Type": "application/json", **(auth.get("header") or {})}


def _call(method: str, path: str, body: dict | None = None, *, session: tuple[str, dict] | None = None):
    host, headers = session or _session()
    request = urllib.request.Request(
        f"{host}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
        headers=headers)
    with urllib.request.urlopen(request) as response:  # noqa: S310 - the configured Conductor server
        text = response.read().decode("utf-8")
    try:
        # Agent output stored in a workflow can carry raw control characters.
        return json.loads(text, strict=False)
    except ValueError:
        return text


def _running_workflow_ids(session=None) -> list[str]:
    ids = []
    for status in ("RUNNING", "PAUSED"):
        start = 0
        while True:
            query = urllib.parse.urlencode({"query": f"status={status}", "size": _PAGE, "start": start})
            results = (_call("GET", f"/workflow/search?{query}", session=session) or {}).get("results") or []
            ids += [str(r.get("workflowId")) for r in results if str(r.get("workflowId")) not in ids]
            if len(results) < _PAGE:
                break
            start += _PAGE
    return ids


def reclaim(task_names: Iterable[str], *, identity: str, before_ms: int) -> list[str]:
    """Requeue every task left behind. One workflow that cannot be read never stops the rest."""
    names, reclaimed, session = list(task_names), [], _session()
    for workflow_id in _running_workflow_ids(session):
        try:
            for task in orphaned(_call("GET", f"/workflow/{workflow_id}?includeTasks=true", session=session), names,
                                 identity=identity, before_ms=before_ms):
                _call("POST", "/tasks", {"workflowInstanceId": workflow_id, "taskId": task["taskId"],
                                         "status": "IN_PROGRESS", "callbackAfterSeconds": 0,
                                         "workerId": identity}, session=session)
                reclaimed.append(str(task["taskId"]))
                log.warning("requeued %s (%s) left behind by the previous worker generation",
                            task.get("referenceTaskName"), task["taskId"])
        except Exception:  # noqa: BLE001
            log.exception("could not check workflow %s for tasks left behind", workflow_id)
    return reclaimed


def reclaim_in_background(task_names: Iterable[str], *, identity: str, before_ms: int,
                          every_s: float | None = None, stop: threading.Event | None = None) -> threading.Thread:
    """Sweep now and every ``every_s`` seconds for as long as this generation lives (or until ``stop``)."""
    period = SWEEP_SECONDS if every_s is None else every_s
    halt = stop or threading.Event()

    def _run() -> None:
        while True:
            try:
                reclaim(task_names, identity=identity, before_ms=before_ms)
            except Exception:  # noqa: BLE001 - a failed sweep must never take the generation down
                log.exception("could not check for tasks left behind")
            if period <= 0 or halt.wait(period):
                return

    thread = threading.Thread(target=_run, name="orphan-reclaim", daemon=True)
    thread.start()
    return thread
