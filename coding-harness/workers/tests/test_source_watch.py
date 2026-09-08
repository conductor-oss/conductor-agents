"""A worker generation must not silently serve code older than the source tree.

A fleet that had been up since before a discovery fix landed kept returning the
pre-fix answer for every run, while the edited file and its bytecode on disk
both looked current -- the failure presented as "the fix does not work".
"""

from __future__ import annotations

import subprocess
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


def test_watcher_defers_the_reload_until_child_work_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(source_watch, "WORKER_ROOT", tmp_path)
    _write(tmp_path, "common/verification.py")
    reloaded: list[bool] = []
    states = iter([True, True, False])
    monkeypatch.setattr(source_watch, "busy", lambda pid=None: next(states, False))
    monkeypatch.setattr(source_watch, "changed_since", lambda *a, **k: ["common/verification.py"])

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
    monkeypatch.setattr(source_watch, "busy", lambda pid=None: False)
    monkeypatch.setattr(source_watch, "changed_since", lambda *a, **k: ["common/verification.py"])

    thread = source_watch.watch(interval=0.01, reload_on_change=False,
                                on_reload=lambda: reloaded.append(True))
    time.sleep(0.2)
    assert reloaded == []
    assert thread.is_alive()
