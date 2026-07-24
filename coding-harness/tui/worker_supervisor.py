"""Lifecycle management for harness workers launched with the TUI."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import BinaryIO

from .auth import credentials_from_env


class WorkerSupervisor:
    """Start the supervised worker poller and stop its whole process group on exit."""

    def __init__(self, server_url: str, root: Path | None = None,
                 log_path: Path | None = None):
        self.server_url = server_url
        self.root = root or Path(__file__).resolve().parents[1]
        self._log_path = log_path or Path.home() / ".conductor-harness" / "workers.log"
        self.process: asyncio.subprocess.Process | None = None
        self.last_error: str | None = None
        self._log: BinaryIO | None = None

    @property
    def script(self) -> Path:
        return self.root / "workers" / "run_workers.sh"

    @property
    def worker_python(self) -> Path:
        return self.root / "workers" / ".venv" / "bin" / "python"

    @property
    def log_path(self) -> Path:
        return self._log_path

    async def start(self) -> bool:
        """Start workers once, inheriting auth without ever logging credential values."""
        if self.process is not None and self.process.returncode is None:
            return True
        if not self.script.is_file():
            self.last_error = f"worker launcher not found: {self.script}"
            return False
        if not self.worker_python.is_file():
            self.last_error = "worker environment missing; run ./run.sh setup"
            return False

        # Enforce the same complete-pair contract as the TUI API client before launch.
        credentials_from_env()
        env = os.environ.copy()
        env["CONDUCTOR_SERVER_URL"] = self.server_url
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = self.log_path.open("ab", buffering=0)
            self.process = await asyncio.create_subprocess_exec(
                "/bin/bash", str(self.script),
                cwd=str(self.root / "workers"),
                env=env,
                stdout=self._log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self.last_error = f"could not start workers: {exc}"
            self._close_log()
            return False
        self.last_error = None
        return True

    async def stop(self) -> None:
        """Stop the launcher and all worker children created in its process group."""
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        self._close_log()

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None
