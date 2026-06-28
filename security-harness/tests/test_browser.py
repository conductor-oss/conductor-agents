"""Browser-driven action capability gate (spec 15.1) in playwright_action.

Only the early-return refusal/scope paths are exercised so no real browser launches.
"""
from browser import tasks as bt


class _Task:
    def __init__(self, data):
        self.input_data = data


def _call(**over):
    base = {"base_url": "http://localhost:3000", "url": "http://localhost:3000/x",
            "scope": {"in_scope_hosts": ["localhost"]}}
    base.update(over)
    return bt.playwright_action(_Task(base))


def test_click_refused_at_capability_1():
    r = _call(action={"type": "click", "selector": "#submit"}, capability_max=1)
    assert "refused: capability" in r["note"]


def test_fill_refused_at_capability_1():
    r = _call(action={"type": "fill", "selector": "#q", "value": "x"}, capability_max=1)
    assert "refused: capability" in r["note"]


def test_out_of_scope_url_refused_before_capability():
    r = _call(url="http://evil.example.org/x",
              action={"type": "click", "selector": "#b"}, capability_max=2)
    assert "out of scope" in r["note"]


def test_missing_capability_defaults_read_only():
    # No capability_max supplied (Conductor sends "" for absent) -> treated as level 1,
    # so a click is refused.
    r = _call(action={"type": "click", "selector": "#b"}, capability_max="")
    assert "refused: capability" in r["note"]
