"""Role-scoped single-instance guard for local worker deployments."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class DuplicateWorkerError(RuntimeError):
    """A different supervisor already polls at least one requested module."""


def _identity(server_url: str, module: str) -> str:
    payload = json.dumps({"server": server_url, "module": module}, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


@contextmanager
def singleton_worker(server_url: str, modules: list[str]) -> Iterator[Path]:
    """Prevent two identical module sets from polling the same Conductor host.

    Locks are one per module, so a broad default deployment cannot overlap a
    narrower ``WORKER_MODULES=coding_agent`` deployment. Distinct roles (for
    example coding agents and the verification-only worker) remain independent.
    Locks are released by the kernel if a supervisor crashes.
    """
    root = Path(os.environ.get("CONDUCTOR_WORKER_LOCK_DIR", tempfile.gettempdir())) / "conductor-worker-locks"
    root.mkdir(parents=True, exist_ok=True)
    handles = []
    paths = []
    def close_in_forked_child() -> None:
        """Keep locks owned by the supervisor, never by task-runner children.

        The Python SDK uses ``fork`` on POSIX.  Without this hook every child
        poller inherits the lock file description, so a crashed supervisor can
        leave an orphan holding the deployment lock and prevent recovery.
        """
        for inherited in handles:
            try:
                inherited.close()
            except OSError:
                pass

    try:
        for module in sorted(set(modules)):
            path = root / f"{_identity(server_url, module)}.lock"
            handle = path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.seek(0)
                owner = handle.read().strip() or "unknown process"
                handle.close()
                raise DuplicateWorkerError(
                    f"duplicate worker deployment refused for module={module} server={server_url}; "
                    f"current owner: {owner}"
                ) from exc
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid(), "module": module, "server": server_url}))
            handle.flush()
            handles.append(handle)
            paths.append(path)
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=close_in_forked_child)
        yield paths[0]
    finally:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
