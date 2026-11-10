"""Hand back tasks a previous worker generation was running when it ended.

A generation restarts when its source changes, and ``source_watch`` only waits for work it can see as
a child process. A deterministic step runs inside the poller, so a restart can end it mid-flight; the
server then holds the task IN_PROGRESS with no worker until its response lease expires. A one-second
``test_plan_resolve`` claimed just before a reload stalled a whole program that way (run 373cac75).

Each new generation therefore requeues what its predecessor left, which also covers a crash. Every
generation polls under its own identity, ``<host>/<start ms>``, so a task is left behind when its
worker is an earlier generation of this host, for a task type this generation polls: pollers are
singletons per deployment (``worker_lock``), so no live process can still hold it. A task claimed under
the bare host name (a worker older than this rule) is judged by its start time instead. The requeue
reports IN_PROGRESS with no callback delay, so it uses none of the task's retries.

The sweep reads every running workflow with its tasks: Conductor's task search cannot filter on the
worker, and a sweep happens once per generation.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request
from typing import Iterable

from conductor.client.http.api_client import ApiClient

from .conductor_config import configuration_from_env

log = logging.getLogger("coding-harness.workers.orphans")
_PAGE = 100


def generation_identity(host: str, started_ms: int) -> str:
    return f"{host}/{int(started_ms)}"


def orphaned(workflow: dict, task_names: Iterable[str], *, identity: str, before_ms: int) -> list[dict]:
    """The tasks of one workflow this generation should requeue (pure)."""
    names, host = set(task_names), identity.split("/", 1)[0]

    def left_behind(task: dict) -> bool:
        worker = str(task.get("workerId") or "")
        if worker.startswith(host + "/"):
            return worker != identity
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


def reclaim_in_background(task_names: Iterable[str], *, identity: str, before_ms: int) -> threading.Thread:
    def _run() -> None:
        try:
            reclaim(task_names, identity=identity, before_ms=before_ms)
        except Exception:  # noqa: BLE001 - a failed sweep must never take the generation down
            log.exception("could not check for tasks left behind")

    thread = threading.Thread(target=_run, name="orphan-reclaim", daemon=True)
    thread.start()
    return thread
