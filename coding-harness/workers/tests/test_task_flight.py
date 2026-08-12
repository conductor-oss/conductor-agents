"""TaskFlight: coalesce Conductor's occasional same-task redelivery.

Confirmed live: two concurrent executions of test_authored_test_commit_verify
ran `git commit` against the same repo for the same task_id; the second saw
nothing left to stage and its "no committable change" result overwrote the
first's real "accepted" one. These tests simulate exactly that race.
"""

from __future__ import annotations

import threading
import time

from common.task_flight import _MISSING, TaskFlight


class _FakeTask:
    def __init__(self, task_id: str, task_def_name: str = "some_task"):
        self.task_id = task_id
        self.task_def_name = task_def_name

    def to_task_result(self, status):
        return _FakeTaskResult(status)


class _FakeTaskResult:
    def __init__(self, status):
        self.status = status
        self.reason_for_incompletion = ""
        self.output_data = {}
        self.logs: list[str] = []

    def log(self, line: str) -> None:
        self.logs.append(line)


def test_claim_grants_ownership_once_and_joins_are_told_to_wait():
    flight = TaskFlight()
    owner1, event1, cached1 = flight.claim("t1")
    owner2, event2, cached2 = flight.claim("t1")

    assert owner1 is True
    assert cached1 is _MISSING
    assert owner2 is False
    assert cached2 is _MISSING
    assert event1 is event2  # joiners wait on the SAME event the owner will set


def test_none_is_a_legitimate_cached_result_not_confused_with_missing():
    # A work() callable that legitimately returns None must round-trip as
    # None, not be mistaken for "nothing cached yet" -- that ambiguity is
    # exactly what caused a real infinite hang before _MISSING existed.
    flight = TaskFlight()
    _, event, _ = flight.claim("t1")
    flight.finish("t1", event, None)

    assert flight.result("t1") is None
    owner, _, cached = flight.claim("t1")
    assert owner is False
    assert cached is None


def test_finish_publishes_the_result_and_wakes_every_joiner():
    flight = TaskFlight()
    _, event, _ = flight.claim("t1")
    flight.finish("t1", event, {"accepted": True})

    assert event.is_set()
    assert flight.result("t1") == {"accepted": True}
    # A late "claim" after finish() joins the cached result directly.
    owner, _, cached = flight.claim("t1")
    assert owner is False
    assert cached == {"accepted": True}


def test_run_executes_work_exactly_once_under_concurrent_duplicate_delivery():
    flight = TaskFlight()
    task = _FakeTask("t1")
    call_count = 0
    started = threading.Event()
    release = threading.Event()

    def slow_work():
        nonlocal call_count
        call_count += 1
        started.set()
        release.wait(2.0)
        return {"accepted": True, "call": call_count}

    results: list[object] = []

    def primary():
        results.append(flight.run(task, slow_work))

    def duplicate():
        started.wait(2.0)  # ensure the primary has already claimed ownership
        results.append(flight.run(task, slow_work))

    t1 = threading.Thread(target=primary)
    t2 = threading.Thread(target=duplicate)
    t1.start()
    t2.start()
    time.sleep(0.1)
    release.set()
    t1.join(2.0)
    t2.join(2.0)

    assert call_count == 1, "the duplicate must join the primary's result, never re-run the work"
    assert len(results) == 2
    assert results[0] == results[1] == {"accepted": True, "call": 1}


def test_run_reports_a_worker_exception_as_a_failed_result_without_raising():
    flight = TaskFlight()
    task = _FakeTask("t1", task_def_name="some_task")

    def boom():
        raise RuntimeError("disk full")

    result = flight.run(task, boom)

    assert result.status == "FAILED"
    assert "disk full" in result.reason_for_incompletion


def test_run_falls_back_to_failed_if_a_waiter_finds_nothing_cached():
    # Defensive-only fallback: finish() always stores a result before setting
    # the event, so this is not reachable through the normal claim/finish
    # sequence. It guards run()'s waiter branch against the event somehow
    # being set without a corresponding cached result.
    flight = TaskFlight()
    task = _FakeTask("t1")
    _, event, _ = flight.claim("t1")
    event.set()  # simulate the event firing with nothing ever cached

    result = flight.run(task, lambda: {"accepted": True})

    assert result.status == "FAILED"
    assert "duplicate delivery" in result.reason_for_incompletion
