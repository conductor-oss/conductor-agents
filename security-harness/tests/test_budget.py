"""Campaign-wide request/data budgets (spec 15.2): counter store + halt enforcement."""
import types

import pytest

from common import budget


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    # budget.bump writes under memory.state_dir() == $STATE_DIR; isolate per test.
    monkeypatch.setenv("STATE_DIR", str(tmp_path))


def test_bump_accumulates_per_run():
    a = budget.bump("run1", requests=1, bytes=100)
    b = budget.bump("run1", requests=1, bytes=50)
    assert a == {"requests": 1, "bytes": 100}
    assert b == {"requests": 2, "bytes": 150}
    assert budget.read("run1") == {"requests": 2, "bytes": 150}


def test_bump_isolates_distinct_runs():
    budget.bump("runA", requests=5)
    budget.bump("runB", requests=2)
    assert budget.read("runA")["requests"] == 5
    assert budget.read("runB")["requests"] == 2


def test_bump_without_run_id_returns_unstored_delta():
    # No run_id -> nowhere to accumulate; the action is still counted within itself.
    assert budget.bump("", requests=3, bytes=9) == {"requests": 3, "bytes": 9}
    assert budget.read("") == {"requests": 0, "bytes": 0}


def _resp(size=10):
    class _Resp:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        text = '{"ok":true}'
        content = b"x" * size
        url = "https://app.test/x"

        class elapsed:  # noqa: N801
            @staticmethod
            def total_seconds():
                return 0.01

    return _Resp()


def test_http_request_trips_request_budget(monkeypatch):
    """With rate.max_requests=2, the 3rd in-scope request flags halt_requested."""
    from httptool import tasks as ht

    monkeypatch.setattr(ht.requests, "request", lambda method, url, **kw: _resp())
    manifest = {"rate": {"max_requests": 2}}
    scope = {"in_scope_hosts": ["app.test"]}

    def fire():
        return ht.http_request(types.SimpleNamespace(input_data={
            "method": "GET", "url": "https://app.test/x", "scope": scope,
            "manifest": manifest, "capability_max": 2, "run_id": "budrun",
        }))

    assert "halt_requested" not in fire()  # 1
    assert "halt_requested" not in fire()  # 2
    tripped = fire()                        # 3 -> over budget
    assert tripped.get("halt_requested")
    assert "request budget" in tripped["halt_requested"]["reason"]
