"""Worker entrypoint for the code_parallel coding harness.

Imports every selected task package so the ``@worker_task`` decorators register
their functions, then starts the Conductor poller. Which task modules load is
controlled by the ``WORKER_MODULES`` env var (comma separated); the default
(``coding_agent,gitops,campaign,openspec,openspecops,automation,model_policy,revision``)
covers every ordinary workflow (including OpenSpec sub-workflows). The
separately deployed read-only ``verification`` worker must run with
``WORKER_MODULES=verification`` so coding workers cannot satisfy their own gate.

    CONDUCTOR_SERVER_URL=http://localhost:8080/api python main.py

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
from pathlib import Path

from conductor.client.automator.task_handler import TaskHandler
from common import check_execution
from common.conductor_config import configuration_from_env
from common.polling import configure_registered_workers, configure_sdk_polling, poll_interval_ms, poll_timeout_ms
from common.worker_lock import DuplicateWorkerError, singleton_worker

DEFAULT_MODULES = "coding_agent,gitops,campaign,openspec,openspecops,automation,model_policy,revision,planning"

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
    modules = [m.strip() for m in os.environ.get("WORKER_MODULES", DEFAULT_MODULES).split(",") if m.strip()]
    for mod in modules:
        importlib.import_module(mod)
        log.info("loaded worker module: %s", mod)
    configure_registered_workers()

    config = configuration_from_env()
    auth_mode = "key/secret" if config.authentication_settings is not None else "none"
    log.info("polling Conductor at %s (authentication=%s)",
             os.environ.get("CONDUCTOR_SERVER_URL", "<unset>"), auth_mode)
    log.info("poll configuration: longPollMs=%s emptyQueueIntervalMs=%s", poll_timeout_ms(), poll_interval_ms())
    server_url = os.environ.get("CONDUCTOR_SERVER_URL", "<unset>")
    try:
        with singleton_worker(server_url, modules) as lock_path:
            log.info("acquired worker deployment lock: %s", lock_path)
            with TaskHandler(configuration=config, scan_for_annotated_workers=True) as handler:
                handler.start_processes()
                handler.join_processes()
    except DuplicateWorkerError as exc:
        log.error("%s", exc)
        # The shell supervisor treats 75 as a deliberate no-op, not a crash to
        # restart every five seconds.
        raise SystemExit(75) from exc


if __name__ == "__main__":
    main()
