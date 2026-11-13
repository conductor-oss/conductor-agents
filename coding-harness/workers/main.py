"""Worker entrypoint for the code_parallel coding harness.

Imports every selected task package so the ``@worker_task`` decorators register
their functions, then starts the Conductor poller. Which task modules load is
controlled by the ``WORKER_MODULES`` env var (comma separated); the default
(``coding_agent,gitops,campaign,openspec,openspecops,automation,model_policy``)
covers every ordinary workflow (including OpenSpec sub-workflows). The
separately deployed read-only ``verification`` worker must run with
``WORKER_MODULES=verification`` so coding workers cannot satisfy their own gate.

    CONDUCTOR_SERVER_URL=http://localhost:8080/api python main.py

A running fleet keeps whatever code it imported at startup, so ``common.source_watch``
recycles the generation when ``workers/**`` changes underneath it (the supervisor
relaunches it). It waits until no poller has child work in flight. Set
``WORKER_RELOAD_ON_CHANGE=false`` to only log the drift, or
``WORKER_SOURCE_CHECK_SECONDS`` to change the 60s poll.

``coding_agent`` drives the Claude Agent SDK / OpenAI Codex / Google Gemini sessions
(CPU/RAM-heavy); ``gitops`` holds the lightweight git + GitHub (gh) tasks;
``openspec``/``openspecops`` shell out to the `openspec` CLI (must be installed on
the worker host — see coding-harness/README.md prerequisites). Split them across
hosts with ``WORKER_MODULES`` per host if desired.
"""

from __future__ import annotations

import importlib
import logging
import os
import signal
import socket
import time
from pathlib import Path

from conductor.client.automator.task_handler import TaskHandler
from common import backend_auth, check_execution
from common.conductor_config import configuration_from_env
from common.polling import configure_registered_workers, configure_sdk_polling, poll_interval_ms, poll_timeout_ms
from common import orphans, source_watch
from common.worker_lock import DuplicateWorkerError, singleton_worker

DEFAULT_MODULES = "coding_agent,gitops,campaign,team,swarm,stages,architect,openspec,openspecops,automation,model_policy,planning"

# Direct ``python workers/main.py`` launches previously resolved Python from a
# virtualenv, but does not add that virtualenv's bin directory for subprocesses.
# Keep Python tools (notably pytest) on the same runtime surface as the worker.
_venv_bin = Path(__file__).resolve().parent / ".venv" / "bin"
if _venv_bin.is_dir() and str(_venv_bin) not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = f"{_venv_bin}{os.pathsep}{os.environ.get('PATH', '')}"

# This runs in the multiprocessing spawn bootstrap too, before the SDK creates
# any TaskRunner.  The SDK otherwise hard-codes a 100ms batch-poll timeout.
configure_sdk_polling()


def main() -> None:
    # Own one process group for this TaskHandler and every poller it spawns.
    # The shell supervisor can then remove the whole generation after a reload;
    # otherwise pollers are reparented to PID 1 and continue serving stale code.
    if os.name == "posix" and os.getpgrp() != os.getpid():
        os.setpgrp()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("coding-harness.workers")

    # Resolve host runtimes once before TaskHandler forks pollers. Every child
    # process (agent CLI, OpenSpec CLI, and deterministic check runner) then
    # inherits the same JDK even under launchd/systemd with no interactive shell.
    java_home = check_execution.install_runtime_environment()
    log.info("worker Java runtime: %s", java_home or "unavailable")

    # Do not permit an operator environment to turn the server's normalized
    # response lease into a hidden execution deadline.
    os.environ["CONDUCTOR_WORKER_ALL_LEASE_EXTEND_ENABLED"] = "true"
    # Every poller of this generation knows its leader: the drain marker names the generation it
    # pauses, so a successor started beside a draining predecessor polls at once.
    os.environ[source_watch.LEADER_ENV] = str(os.getpid())
    source_watch.end_drain()  # a marker of this generation's own (or of a dead one) is cleared; it polls
    modules = [m.strip() for m in os.environ.get("WORKER_MODULES", DEFAULT_MODULES).split(",") if m.strip()]
    for mod in modules:
        importlib.import_module(mod)
        log.info("loaded worker module: %s", mod)
    configure_registered_workers()
    # Every task this generation claims carries its identity, so the next generation can tell what its
    # predecessor left behind without comparing clocks (``common.orphans``).
    from conductor.client.automator.task_handler import _decorated_functions
    generation_started_ms = int(time.time() * 1000)
    identity = orphans.generation_identity(socket.gethostname(), generation_started_ms, os.getpid())
    for record in _decorated_functions.values():
        record["worker_id"] = identity

    config = configuration_from_env()
    auth_mode = "key/secret" if config.authentication_settings is not None else "none"
    log.info("polling Conductor at %s (authentication=%s)",
             os.environ.get("CONDUCTOR_SERVER_URL", "<unset>"), auth_mode)
    log.info("poll configuration: longPollMs=%s emptyQueueIntervalMs=%s", poll_timeout_ms(), poll_interval_ms())
    for line in backend_auth.report_lines():
        log.info("backend credential: %s", line)
    server_url = os.environ.get("CONDUCTOR_SERVER_URL", "<unset>")
    try:
        with singleton_worker(server_url, modules) as lock_path:
            log.info("acquired worker deployment lock: %s", lock_path)
            with TaskHandler(configuration=config, scan_for_annotated_workers=True) as handler:
                handler.start_processes()
                orphans.reclaim_in_background([name for name, _domain in _decorated_functions],
                                              identity=identity, before_ms=generation_started_ms)
                # A fleet outlives the source it loaded. Without this, an edit
                # reaches nothing until someone restarts the generation by
                # hand, and the stale answer is indistinguishable from a wrong
                # fix. The supervisor relaunches whatever this leader leaves.
                def _handover() -> bool:
                    from common import worker_lock
                    worker_lock.release_all()  # the successor takes the deployment locks
                    return source_watch.start_successor(leader=os.getpid())

                source_watch.watch(on_reload=lambda: os.kill(os.getpid(), signal.SIGTERM),
                                   on_handover=_handover)
                handler.join_processes()
    except DuplicateWorkerError as exc:
        log.error("%s", exc)
        # The shell supervisor treats 75 as a deliberate no-op, not a crash to
        # restart every five seconds.
        raise SystemExit(75) from exc


if __name__ == "__main__":
    main()
