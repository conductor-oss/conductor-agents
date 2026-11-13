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


def _children_map() -> dict[int, list[tuple[int, str]]]:
    """``parent pid -> [(child pid, command)]`` for every process on the host.

    The listing runs through the shared runtime helper like every other command
    the harness launches, so it inherits the same environment and process-group
    ownership rather than opening a second, unaudited execution surface.
    """
    try:
        listing = run(["ps", "-eo", "pid=,ppid=,command="], check=False).stdout
    except Exception:  # noqa: BLE001 - a probe failure must never stop the fleet
        return {}
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
    return children


def _descendant_commands(pid: int) -> list[str]:
    """Command lines of every descendant of ``pid`` (the poller's own tree)."""
    children = _children_map()
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


def generation_busy(leader: int) -> bool:
    """True while any poller of the generation led by ``leader`` has child work.

    The leader's direct children are the pollers themselves, one per task type,
    and they exist for the generation's whole life. Counting them as work made
    every production generation look permanently busy: the coding fleet logged
    "deferring reload" every minute while idle (leader 60592, 2026-11-07) and an
    earlier fleet served two-month-old code. Work is what runs *below* a poller:
    an agent CLI session, a build, a test run.
    """
    children = _children_map()
    # The successor supervisor (``start_successor``) is a child of the leader too, and its own
    # generation runs below it; counting it kept every predecessor "draining" for as long as its
    # successor lived (three generations stacked up in one hour, 2026-11-10).
    return any(children.get(poller) for poller, command in children.get(leader, [])
               if _SUPERVISOR_SCRIPT not in command)


# While a reload is pending the generation's pollers claim no new task (``common.polling`` honours
# the marker), so in-flight work drains while a NEW generation, started at once beside it, takes
# every new task: no stall. The marker names the draining generation's leader, so the successor's
# pollers ignore it. Before the handover, every edit paused the whole fleet for up to one agent
# session (the drain), and before the drain a busy fleet never reloaded at all (2026-11-10).
DRAIN_MARKER = ".reload-pending"
_SUPERVISOR_SCRIPT = "run_workers.sh"
# A long poll already in flight when the marker is written can still return a task, and a step that
# runs inside the poller has no child process to show for it until it starts: a generation that
# reloaded 75 ms after writing its marker took a just-claimed `model_profile_resolve` with it and the
# program's final test cycle waited 13 minutes on the task (run 884f7eec). The marker must be older
# than one long poll before the generation may end.
DRAIN_GRACE_SECONDS = 15.0
LEADER_ENV = "WORKER_LEADER_PID"        # set by main.py before the pollers fork; inherited by them
HANDOVER_ENV = "WORKER_HANDOVER_FROM"   # the successor learns which leader it replaces


def _leader(leader: int | None = None) -> int:
    return leader if leader is not None else int(os.environ.get(LEADER_ENV) or os.getpid())


def drain_marker(root: Path | None = None, leader: int | None = None) -> Path:
    """One marker per draining generation, named after its leader: generations drain side by side
    (every edit starts one more), and a shared marker, overwritten by the newest, let the older
    generations resume claiming work under the code they were meant to stop serving."""
    return (root or WORKER_ROOT) / f"{DRAIN_MARKER}-{_leader(leader)}"


def _markers(root: Path | None = None) -> list[tuple[int, Path]]:
    out = []
    for path in (root or WORKER_ROOT).glob(f"{DRAIN_MARKER}-*"):
        try:
            out.append((int(path.name.rsplit("-", 1)[1]), path))
        except ValueError:
            continue
    return out


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def draining(root: Path | None = None, *, leader: int | None = None) -> bool:
    """True for the pollers of a generation whose leader wrote a marker (this one, by default):
    they claim no new work."""
    return drain_marker(root, leader).exists()


def drain_age(root: Path | None = None, *, leader: int | None = None) -> float:
    """Seconds since this generation's marker was written; 0 without one."""
    try:
        return max(0.0, time.time() - float(drain_marker(root, leader).read_text(encoding="utf-8").split()[1]))
    except (OSError, ValueError, IndexError):
        return 0.0


def begin_drain(root: Path | None = None, *, leader: int | None = None) -> None:
    try:
        drain_marker(root, leader).write_text(f"{_leader(leader)} {time.time()}", encoding="utf-8")
    except OSError:
        pass


def end_drain(root: Path | None = None, *, leader: int | None = None) -> None:
    """Drop this generation's marker, and every marker of a generation that is gone; a successor
    never removes the marker its predecessor is still draining under."""
    mine = _leader(leader)
    for pid, path in _markers(root):
        if pid == mine or not _pid_alive(pid):
            try:
                path.unlink()
            except OSError:
                pass


def start_successor(*, leader: int, env: dict | None = None, spawner=None) -> bool:
    """Start the next generation beside this one: a new supervisor running the current source, told
    which leader it replaces (``WORKER_HANDOVER_FROM``, so it waits for that leader to exit before
    reclaiming tasks). Returns whether it started."""
    import subprocess
    from . import check_execution
    # The successor is the same supervisor this generation was started as, so it inherits the full
    # worker runtime environment (the repository command runtime policy), told only whom it replaces.
    base = dict(env) if env is not None else check_execution.inherited_environment()
    base[HANDOVER_ENV] = str(leader)
    base.pop(LEADER_ENV, None)
    script = WORKER_ROOT / "run_workers.sh"
    try:
        if spawner is not None:
            spawner([str(script)], base)
        else:
            subprocess.Popen([str(script)], cwd=str(WORKER_ROOT), env=base, start_new_session=True,
                             stdin=subprocess.DEVNULL)
        return True
    except OSError as exc:
        log.error("could not start the successor generation: %s", exc)
        return False


def watch(*, interval: float | None = None, reload_on_change: bool | None = None,
          on_reload=None, root: Path | None = None, on_handover=None) -> threading.Thread:
    """Start the drift watcher. Returns the (already started) daemon thread. ``root`` is the source
    tree watched and where the drain marker lives (the workers directory; a test passes its own).
    ``on_handover`` starts the successor generation once this one has stopped claiming work and
    released its locks (``start_successor`` in production); ``on_reload`` ends this generation once
    its in-flight work has drained."""
    baseline = source_fingerprint(root)
    period = interval if interval is not None else float(
        os.environ.get("WORKER_SOURCE_CHECK_SECONDS") or DEFAULT_INTERVAL_SECONDS)
    reload_enabled = (reload_on_change if reload_on_change is not None
                      else _truthy(os.environ.get("WORKER_RELOAD_ON_CHANGE"), True))
    leader = os.getpid()

    def _run() -> None:
        warned = False
        handed_over = False
        while True:
            time.sleep(period)
            drifted = changed_since(baseline, root)
            if not drifted:
                warned = False
                if draining(root, leader=leader) and not handed_over:
                    end_drain(root, leader=leader)  # a marker with no drift behind it is stale: poll again
                continue
            if not warned:
                log.warning(
                    "worker source changed since this generation started (%d file(s), "
                    "newest first: %s) -- this process is still executing the OLD code",
                    len(drifted), ", ".join(drifted[:5]))
                warned = True
            if not reload_enabled:
                continue
            if not draining(root, leader=leader):
                begin_drain(root, leader=leader)
                log.warning("reload pending: this generation claims no new task; its in-flight work drains")
            if not handed_over and on_handover is not None:
                # Hand the deployment over now: the successor takes every new task while this
                # generation finishes what it holds. Nothing waits for an idle moment.
                try:
                    handed_over = bool(on_handover())
                except Exception:  # noqa: BLE001
                    log.exception("handover to a successor generation failed; draining to a reload instead")
                if handed_over:
                    log.warning("successor generation started; this one exits when its work is done")
            if generation_busy(leader):
                log.warning("deferring reload: child work still running in this generation (draining)")
                continue
            if drain_age(root, leader=leader) < DRAIN_GRACE_SECONDS:
                continue  # a poll in flight when the marker was written may still hand this generation a task
            log.warning("reloading worker generation to pick up changed source")
            if on_reload is not None:
                on_reload()
            return

    thread = threading.Thread(target=_run, name="source-watch", daemon=True)
    thread.start()
    return thread
