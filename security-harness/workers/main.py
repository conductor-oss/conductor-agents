"""Worker entrypoint.

Imports every task package so the ``@worker_task`` decorators register their
functions, then starts the Conductor poller. Runs identically on the host
(``python main.py`` from this dir) or inside the worker Docker image.

Which task modules load is controlled by the WORKER_MODULES env var (comma
separated), defaulting to the Phase-0 base worker. Later phases add
``browser``, ``dast``, ``sast``, ``api`` packages here.
"""

from __future__ import annotations

import importlib
import logging
import os

from conductor.client.automator.task_handler import TaskHandler
from conductor.client.configuration.configuration import Configuration


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("security-conductor.workers")

    modules = [m.strip() for m in os.environ.get("WORKER_MODULES", "recon").split(",") if m.strip()]
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
