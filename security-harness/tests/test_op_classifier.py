"""The sandbox operation classifier stays product-neutral without profile rules."""
import json
import os

from codeexec import sandbox_sc as sc

VULN_APP_PROFILE = json.load(
    open(os.path.join(os.path.dirname(__file__), "..", "profiles", "vuln-app.json"))
)

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


def test_vuln_app_profile_keeps_engine_product_neutral(monkeypatch):
    ops = _run(monkeypatch, VULN_APP_PROFILE.get("feature_operations", []))
    # No product API knowledge in the engine: every recognized call is a generic product_write,
    # and the GET read produces no op (reads aren't writes).
    assert all(o["type"] == "product_write" for o in ops)
    assert {o["path"] for o in ops} == {"/api/metadata/workflow", "/api/workflow/wf1", "/api/secrets/foo"}
    assert not any(o["type"].startswith("workflow_") for o in ops)
