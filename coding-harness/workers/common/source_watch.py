"""Detect worker processes running code older than the source tree on disk.

Python imports a module once per process. ``run_workers.sh`` keeps a fleet up
for days, so an edit to ``workers/**`` reaches nothing until the generation is
restarted -- and nothing said so. A fleet that has been up since before a fix
landed keeps producing the pre-fix answer while the file and its ``.pyc`` on
disk both look current, which reads as "the fix did not work" rather than "the
fix is not running".

The watcher records the newest source mtime at import time and re-checks on an
interval. Drift is always logged. Reloading is opt-out rather than opt-in --
serving known-stale code silently is the worse failure -- but a generation is
only recycled while it is idle: a live coding-agent session represents many
minutes of paid work, so the reload waits for it to finish.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from .exec import run

log = logging.getLogger("coding-harness.workers.source")

WORKER_ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {".venv", "__pycache__", ".git", ".pytest_cache", "tests"}
DEFAULT_INTERVAL_SECONDS = 60.0
_PROBE_MARKER = "-eo pid=,ppid=,command="


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def source_fingerprint(root: Path | None = None) -> tuple[float, int]:
    """Newest mtime and file count across the worker's own Python sources.

    ``tests`` is excluded: editing a test never changes what a running poller
    executes, and recycling a fleet mid-run for a test edit is pure loss.
    """
    base = root or WORKER_ROOT
    newest = 0.0
    count = 0
    for path in base.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.relative_to(base).parts):
            continue
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
        count += 1
    return newest, count


def changed_since(baseline: tuple[float, int], root: Path | None = None) -> list[str]:
    """Source files newer than the baseline fingerprint, newest first."""
    base = root or WORKER_ROOT
    cutoff = baseline[0]
    drifted: list[tuple[float, str]] = []
    for path in base.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.relative_to(base).parts):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            drifted.append((mtime, str(path.relative_to(base))))
    return [name for _, name in sorted(drifted, reverse=True)]


def _descendant_commands(pid: int) -> list[str]:
    """Command lines of every descendant of ``pid`` (the poller's own tree).

    The listing runs through the shared runtime helper like every other command
    the harness launches, so it inherits the same environment and process-group
    ownership rather than opening a second, unaudited execution surface.
    """
    try:
        listing = run(["ps", "-eo", "pid=,ppid=,command="], check=False).stdout
    except Exception:  # noqa: BLE001 - a probe failure must never stop the fleet
        return []
    children: dict[int, list[tuple[int, str]]] = {}
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            child, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        # The probe is a child of this process for as long as it runs, so it
        # appears in its own output; counting it would make every generation
        # look permanently busy and defer the reload forever.
        if _PROBE_MARKER in parts[2]:
            continue
        children.setdefault(parent, []).append((child, parts[2]))
    found: list[str] = []
    queue = [pid]
    seen = {pid}
    while queue:
        for child, command in children.get(queue.pop(), []):
            if child in seen:
                continue
            seen.add(child)
            queue.append(child)
            found.append(command)
    return found


def busy(pid: int | None = None) -> bool:
    """True while this worker generation still has child work running.

    Every task that costs real money or real time (an agent CLI session, a
    build, a test run) executes as a subprocess of a poller, so an empty
    descendant set is a sound "nothing in flight" signal for the generation.
    """
    return bool(_descendant_commands(pid if pid is not None else os.getpid()))


def watch(*, interval: float | None = None, reload_on_change: bool | None = None,
          on_reload=None) -> threading.Thread:
    """Start the drift watcher. Returns the (already started) daemon thread."""
    baseline = source_fingerprint()
    period = interval if interval is not None else float(
        os.environ.get("WORKER_SOURCE_CHECK_SECONDS") or DEFAULT_INTERVAL_SECONDS)
    reload_enabled = (reload_on_change if reload_on_change is not None
                      else _truthy(os.environ.get("WORKER_RELOAD_ON_CHANGE"), True))
    leader = os.getpid()

    def _run() -> None:
        warned = False
        while True:
            time.sleep(period)
            drifted = changed_since(baseline)
            if not drifted:
                warned = False
                continue
            if not warned:
                log.warning(
                    "worker source changed since this generation started (%d file(s), "
                    "newest first: %s) -- this process is still executing the OLD code",
                    len(drifted), ", ".join(drifted[:5]))
                warned = True
            if not reload_enabled:
                continue
            if busy(leader):
                log.warning("deferring reload: child work still running in this generation")
                continue
            log.warning("reloading worker generation to pick up changed source")
            if on_reload is not None:
                on_reload()
            return

    thread = threading.Thread(target=_run, name="source-watch", daemon=True)
    thread.start()
    return thread
