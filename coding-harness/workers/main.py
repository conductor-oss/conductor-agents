"""Worker entrypoint for the code_parallel coding harness.

Imports every selected task package so the ``@worker_task`` decorators register
their functions, then starts the Conductor poller. Which task modules load is
controlled by the ``WORKER_MODULES`` env var (comma separated); the default
(``coding_agent,gitops,checks``) covers every workflow (code_parallel, issue_to_pr,
pr_review, address_pr, github_demo, and the design_docs / code_subtask sub-workflows).

    CONDUCTOR_SERVER_URL=http://localhost:8080/api python main.py

``coding_agent`` drives the Claude Agent SDK / OpenAI Codex / Google Gemini sessions
(CPU/RAM-heavy); ``gitops`` holds the lightweight git + GitHub (gh) tasks; ``checks``
holds the LLM-free ``run_checks`` verification-gate task. Split them across hosts with
``WORKER_MODULES`` per host if desired.
"""

from __future__ import annotations

import importlib
import logging
import os

from conductor.client.automator.task_handler import TaskHandler
from conductor.client.configuration.configuration import Configuration

DEFAULT_MODULES = "coding_agent,gitops,checks"


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("coding-harness.workers")

    modules = [m.strip() for m in os.environ.get("WORKER_MODULES", DEFAULT_MODULES).split(",") if m.strip()]
    for mod in modules:
        importlib.import_module(mod)
        log.info("loaded worker module: %s", mod)

    config = Configuration()
    log.info("polling Conductor at %s", os.environ.get("CONDUCTOR_SERVER_URL", "<unset>"))
    with TaskHandler(configuration=config, scan_for_annotated_workers=True) as handler:
        handler.start_processes()
        handler.join_processes()


if __name__ == "__main__":
    main()
