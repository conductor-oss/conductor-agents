"""The sandbox operation classifier is now DATA-DRIVEN (product-neutral engine): classification
rules come from SC_FEATURE_OPS / a profile's feature_operations, not hardcoded product API paths.
Proves (a) with no rules the engine emits only generic product_write (no product specifics), and
(b) the conductor profile's rules reproduce the workflow_registered/started/polled op types exactly
(no regression for the Conductor case)."""
import json
import os

from codeexec import sandbox_sc as sc

CONDUCTOR_RULES = json.load(open(os.path.join(os.path.dirname(__file__), "..", "profiles", "conductor.json")))["feature_operations"]

CALLS = [
    ("POST", "https://t/api/metadata/workflow", {"name": "wf1", "tasks": [{"type": "HTTP"}, {"type": "INLINE"}]}, 200, None, ""),
    ("POST", "https://t/api/workflow/wf1", None, 200, {"workflowId": "exec-123"}, ""),
    ("GET", "https://t/api/workflow/exec-123", None, 200, None, ""),
    ("PUT", "https://t/api/secrets/foo", "x", 200, None, ""),
]


def _run(monkeypatch, rules):
    monkeypatch.setattr(sc, "_FEATURE_OPS", rules)
    monkeypatch.setattr(sc, "flush", lambda: None)
    sc._state["operations"] = []
    for c in CALLS:
        sc._record_api_operation(*c)
    return sc._state["operations"]


def test_no_rules_engine_is_product_neutral(monkeypatch):
    ops = _run(monkeypatch, [])
    # No product API knowledge in the engine: every recognized call is a generic product_write,
    # and the GET read produces no op (reads aren't writes). Zero Conductor op types.
    assert all(o["type"] == "product_write" for o in ops)
    assert {o["path"] for o in ops} == {"/api/metadata/workflow", "/api/workflow/wf1", "/api/secrets/foo"}
    assert not any(o["type"].startswith("workflow_") for o in ops)


def test_conductor_profile_rules_reproduce_op_types(monkeypatch):
    ops = _run(monkeypatch, CONDUCTOR_RULES)
    by_type = {o["type"]: o for o in ops}
    assert by_type["workflow_registered"]["workflow_name"] == "wf1"
    assert by_type["workflow_registered"]["task_types"] == ["HTTP", "INLINE"]
    assert by_type["workflow_started"]["workflow_name"] == "wf1" and by_type["workflow_started"]["execution_id"] == "exec-123"
    assert by_type["workflow_polled"]["execution_id"] == "exec-123"
    assert by_type["product_write"]["path"] == "/api/secrets/foo"   # unmatched write stays generic
