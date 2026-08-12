"""ProgressReporter: mid-execution IN_PROGRESS heartbeats for long-running tasks.

No live Conductor server: _api() is monkeypatched to a fake TaskResourceApi that
just records update_task calls.
"""

from __future__ import annotations

import time

from common import progress
from common.progress import ProgressReporter


class _FakeTaskResult:
    def __init__(self):
        self.output_data = None


class _FakeTask:
    """Minimal stand-in for a conductor Task -- only to_task_result is used."""

    def to_task_result(self, _status):
        return _FakeTaskResult()


class _FakeApi:
    def __init__(self):
        self.pushed: list[dict] = []

    def update_task(self, task_result):
        self.pushed.append(task_result.output_data)


def _fake_api(monkeypatch, api: _FakeApi) -> None:
    monkeypatch.setattr(progress, "_api", lambda: api)


def test_update_wakes_the_heartbeat_thread_for_an_immediate_push(monkeypatch):
    api = _FakeApi()
    _fake_api(monkeypatch, api)
    reporter = ProgressReporter(_FakeTask(), heartbeat_s=999).start()
    try:
        reporter.update({"turns": [{"turn": 1}], "numTurns": 1})
        deadline = time.monotonic() + 2.0
        while not api.pushed and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        reporter.stop()

    assert api.pushed, "update() should trigger a push well before the 999s heartbeat"
    pushed = api.pushed[-1]
    assert pushed["numTurns"] == 1
    assert pushed["status"] == "IN_PROGRESS"
    assert pushed["running"] is True
    assert "elapsedSeconds" in pushed


def test_heartbeat_fires_even_without_a_single_update(monkeypatch):
    # The exact scenario this exists for: one command blocks for a very long
    # time with no per-turn signal at all -- the task must still not look frozen.
    # The loop's own wake-wait blocks up to 2s between checks regardless of
    # heartbeat_s, so the deadline here must clear that, not just heartbeat_s.
    api = _FakeApi()
    _fake_api(monkeypatch, api)
    reporter = ProgressReporter(_FakeTask(), heartbeat_s=0.05).start()
    try:
        deadline = time.monotonic() + 4.0
        while not api.pushed and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        reporter.stop()

    assert api.pushed, "the heartbeat loop must push a snapshot even with no update() calls"


def test_a_stale_snapshot_never_overwrites_a_newer_one(monkeypatch):
    # The per-turn (update) and heartbeat paths can race; an older snapshot
    # arriving last would make progress appear to jump backwards.
    api = _FakeApi()
    _fake_api(monkeypatch, api)
    reporter = ProgressReporter(_FakeTask(), heartbeat_s=999)
    reporter.update({"numTurns": 1})
    reporter._push()
    reporter.update({"numTurns": 2})
    reporter._push()
    # Simulate a stale, already-superseded push being retried out of order.
    reporter._seq -= 1
    reporter._pushed_seq = 2
    reporter._push()

    assert [snap["numTurns"] for snap in api.pushed] == [1, 2]


def test_push_failures_never_raise(monkeypatch):
    class _BrokenApi:
        def update_task(self, _task_result):
            raise RuntimeError("network is down")

    monkeypatch.setattr(progress, "_api", lambda: _BrokenApi())
    reporter = ProgressReporter(_FakeTask(), heartbeat_s=999)
    reporter.update({"numTurns": 1})
    reporter._push()  # must not raise -- progress reporting must never break the task
