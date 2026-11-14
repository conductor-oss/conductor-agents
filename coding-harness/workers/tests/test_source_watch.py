"""A worker generation must not silently serve code older than the source tree.

A fleet that had been up since before a discovery fix landed kept returning the
pre-fix answer for every run, while the edited file and its bytecode on disk
both looked current -- the failure presented as "the fix does not work".
"""

from __future__ import annotations

import subprocess
import os
import time

from common import source_watch


def _write(root, name: str, text: str = "x = 1\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_fingerprint_counts_sources_and_ignores_generated_and_test_trees(tmp_path):
    _write(tmp_path, "common/verification.py")
    _write(tmp_path, "gitops/tasks.py")
    _write(tmp_path, "__pycache__/verification.cpython-314.pyc")
    _write(tmp_path, ".venv/lib/thing.py")
    _write(tmp_path, "tests/test_thing.py")

    newest, count = source_watch.source_fingerprint(tmp_path)
    assert count == 2
    assert newest > 0


def test_editing_a_source_file_is_reported_as_drift(tmp_path):
    _write(tmp_path, "common/verification.py")
    baseline = source_watch.source_fingerprint(tmp_path)
    assert source_watch.changed_since(baseline, tmp_path) == []

    time.sleep(0.01)
    edited = _write(tmp_path, "common/verification.py", "x = 2\n")
    import os
    os.utime(edited, (baseline[0] + 5, baseline[0] + 5))

    assert source_watch.changed_since(baseline, tmp_path) == ["common/verification.py"]


def test_editing_a_test_never_recycles_a_generation(tmp_path):
    """Recycling a fleet for a test edit costs a run and changes no behaviour."""
    _write(tmp_path, "common/verification.py")
    baseline = source_watch.source_fingerprint(tmp_path)
    import os
    edited = _write(tmp_path, "tests/test_thing.py", "x = 2\n")
    os.utime(edited, (baseline[0] + 5, baseline[0] + 5))

    assert source_watch.changed_since(baseline, tmp_path) == []


def test_busy_tracks_live_child_work_and_excludes_its_own_probe():
    """The probe is a child of the caller while it runs, so it must be excluded."""
    assert source_watch.busy() is False

    child = subprocess.Popen(["sleep", "30"])
    try:
        time.sleep(0.2)
        assert source_watch.busy() is True
    finally:
        child.kill()
        child.wait()
    time.sleep(0.2)
    assert source_watch.busy() is False


def test_idle_pollers_do_not_make_a_generation_busy():
    """A generation is leader -> one poller per task type -> (work). The pollers are always
    there, so a watcher that counted them deferred the reload forever (coding fleet 60592
    logged "deferring reload" every minute while fully idle, 2026-11-07). Only a process
    *below* a poller is work."""
    import os
    import sys

    idle_poller = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.3)
        assert source_watch.generation_busy(os.getpid()) is False
        working_poller = subprocess.Popen(
            [sys.executable, "-c", "import subprocess, time; subprocess.Popen(['sleep', '30']); time.sleep(30)"])
        try:
            time.sleep(0.6)
            assert source_watch.generation_busy(os.getpid()) is True
        finally:
            for child in source_watch._children_map().get(working_poller.pid, []):
                os.kill(child[0], 9)
            working_poller.kill()
            working_poller.wait()
        time.sleep(0.3)
        assert source_watch.generation_busy(os.getpid()) is False
    finally:
        idle_poller.kill()
        idle_poller.wait()


def test_watcher_defers_the_reload_until_child_work_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(source_watch, "WORKER_ROOT", tmp_path)
    _write(tmp_path, "common/verification.py")
    reloaded: list[bool] = []
    states = iter([True, True, False])
    monkeypatch.setattr(source_watch, "generation_busy", lambda leader: next(states, False))
    monkeypatch.setattr(source_watch, "changed_since", lambda *a, **k: ["common/verification.py"])
    monkeypatch.setattr(source_watch, "DRAIN_GRACE_SECONDS", 0.0)

    thread = source_watch.watch(interval=0.01, reload_on_change=True,
                                on_reload=lambda: reloaded.append(True))
    for _ in range(500):
        if reloaded:
            break
        time.sleep(0.01)
    assert reloaded == [True]


def test_reload_can_be_disabled_leaving_only_the_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(source_watch, "WORKER_ROOT", tmp_path)
    _write(tmp_path, "common/verification.py")
    reloaded: list[bool] = []
    monkeypatch.setattr(source_watch, "generation_busy", lambda leader: False)
    monkeypatch.setattr(source_watch, "changed_since", lambda *a, **k: ["common/verification.py"])

    thread = source_watch.watch(interval=0.01, reload_on_change=False,
                                on_reload=lambda: reloaded.append(True))
    time.sleep(0.2)
    assert reloaded == []
    assert thread.is_alive()


def test_a_pending_reload_drains_the_generation_instead_of_waiting_for_idle(tmp_path, monkeypatch):
    """A busy fleet is never idle: once the source changed, the generation's pollers claim no new task
    until the in-flight work finishes, and the next generation starts polling again (run 351812b2's
    scouts ran hour-old code while every fix sat on disk)."""
    from common import polling
    monkeypatch.setattr(source_watch, "WORKER_ROOT", tmp_path)
    assert not source_watch.draining() and not polling._draining()
    source_watch.begin_drain()
    assert source_watch.draining() and polling._draining()
    source_watch.end_drain()
    assert not polling._draining()


def test_handover_starts_a_successor_beside_the_draining_generation(tmp_path, monkeypatch):
    """A reload must not stall the fleet: the draining generation names itself in the marker (its own
    pollers stop claiming), releases its locks, and starts the successor at once; the successor's
    pollers ignore the marker and a dead generation's marker is nobody's. Markers are per generation:
    generations drain side by side (one per edit), and one shared marker, overwritten by the newest,
    let the older ones resume claiming work under stale code (three stacked up in an hour, 2026-11-10)."""
    monkeypatch.setattr(source_watch, "WORKER_ROOT", tmp_path)
    monkeypatch.setattr(source_watch, "_pid_alive", lambda pid: pid == 111)
    source_watch.begin_drain(leader=111)
    assert source_watch.draining(leader=111) is True and source_watch.draining(leader=222) is False
    source_watch.end_drain(leader=222)          # a successor never removes its predecessor's marker
    assert source_watch.draining(leader=111) is True
    source_watch.end_drain(leader=111)          # the owner drops it
    assert not source_watch.draining(leader=111)
    source_watch.begin_drain(leader=999)        # a marker of a generation that is gone
    source_watch.end_drain(leader=222)
    assert not source_watch.draining(leader=999)
    source_watch.begin_drain(leader=111)
    source_watch.begin_drain(leader=os.getpid())  # a second draining generation does not free the first
    assert source_watch.draining(leader=111) and source_watch.draining(leader=os.getpid())
    assert source_watch.drain_age(leader=111) < 5 and source_watch.drain_age(leader=424242) == 0
    source_watch.end_drain(leader=111)
    source_watch.end_drain(leader=os.getpid())
    # The successor is started with the leader it replaces, without inheriting the leader's identity.
    spawned = []
    monkeypatch.setenv(source_watch.LEADER_ENV, "111")
    assert source_watch.start_successor(leader=111, spawner=lambda argv, env: spawned.append((argv, env))) is True
    argv, env = spawned[0]
    assert argv[0].endswith("run_workers.sh") and env[source_watch.HANDOVER_ENV] == "111" and source_watch.LEADER_ENV not in env
    # The watcher hands over as soon as drift is seen, then ends this generation when its work drains.
    monkeypatch.setattr(source_watch, "changed_since", lambda *a, **k: ["common/x.py"])
    states = iter([True, False])
    monkeypatch.setattr(source_watch, "generation_busy", lambda leader: next(states, False))
    # The generation may end only once its marker is older than a long poll: a poll in flight when
    # the marker was written can still hand it a task (a `model_profile_resolve` claimed 75 ms before
    # its generation reloaded stalled a program's final test cycle for 13 minutes, run 884f7eec).
    monkeypatch.setattr(source_watch, "DRAIN_GRACE_SECONDS", 0.3)
    events = []
    started = time.time()
    thread = source_watch.watch(interval=0.01, reload_on_change=True, root=tmp_path,
                                on_handover=lambda: events.append("handover") or True,
                                on_reload=lambda: events.append(("reload", time.time() - started)))
    for _ in range(500):
        if any(e[0] == "reload" for e in events if isinstance(e, tuple)):
            break
        time.sleep(0.01)
    assert events[0] == "handover" and events[1][0] == "reload"  # one successor, started before the drain finished
    assert events[1][1] >= 0.3


def test_lock_release_all_frees_the_deployment_for_the_successor(tmp_path, monkeypatch):
    from common import worker_lock
    monkeypatch.setenv("CONDUCTOR_WORKER_LOCK_DIR", str(tmp_path))
    with worker_lock.singleton_worker("http://c", ["team"]):
        assert worker_lock.release_all() == 1
        # The successor can take the same deployment while the predecessor still runs.
        with worker_lock.singleton_worker("http://c", ["team"]) as path:
            assert path.exists()
    assert worker_lock.release_all() == 0


def test_a_draining_predecessors_tasks_are_left_to_it_and_a_dead_ones_are_reclaimed(monkeypatch):
    """Two generations run side by side during a handover. The successor requeues only what a generation
    whose leader is GONE claimed (requeued from a live one, a task would run twice); it sweeps for the
    life of the generation, because a predecessor can claim a task after the successor started."""
    from common import orphans
    me = orphans.generation_identity("h", 2000, 77)
    task = {"taskId": "t", "taskDefName": "x", "status": "IN_PROGRESS", "workerId": "h/1000/55", "startTime": 2500}
    pick = lambda t, live: orphans.orphaned({"tasks": [t]}, ["x"], identity=me, before_ms=2000, alive=lambda pid: pid in live)  # noqa: E731
    assert pick(task, live={55}) == []            # the predecessor still runs: its task finishes there
    assert pick(task, live=set()) == [task]       # gone: requeued, whenever it was claimed
    assert pick({**task, "workerId": me}, live=set()) == []
    import threading
    swept, stop = [], threading.Event()
    def sweep(names, **kw):
        swept.append(kw["identity"])
        if len(swept) == 3:
            stop.set()
    monkeypatch.setattr(orphans, "reclaim", sweep)
    thread = orphans.reclaim_in_background(["x"], identity=me, before_ms=2000, every_s=0.01, stop=stop)
    thread.join(2)
    assert swept == [me, me, me] and not thread.is_alive()
