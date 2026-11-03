"""Register/run + task-type instrumentation (item #6): create-and-run / PUT / bulk / inline-def
calls must be classified so `workflows_defined` and `task_types_exercised` stop under-reporting."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
from codeexec import sandbox_sc as s  # noqa: E402
from common import feature_exercise as fe  # noqa: E402

# profiles/conductor.json is intentionally local-only (gitignored; see commit bb3c971), so it is
# absent in a fresh checkout / CI. Load it defensively and skip only the ops-classification tests
# that need its `feature_operations` rules — the profile-independent tests still run everywhere.
_PROFILE_PATH = os.path.join(os.path.dirname(__file__), "..", "profiles", "conductor.json")
try:
    with open(_PROFILE_PATH) as _fh:
        _OPS = json.load(_fh)["feature_operations"]
except (FileNotFoundError, KeyError, ValueError):
    _OPS = None

_needs_profile = pytest.mark.skipif(
    _OPS is None, reason="profiles/conductor.json is local-only (gitignored); run locally for this check")


def _classify(method, url, request_body):
    saved_rules, saved_ops, saved_flush = s._FEATURE_OPS, s._state.get("operations"), s.flush
    s._FEATURE_OPS = _OPS
    s._state["operations"] = []
    s.flush = lambda: None
    try:
        s._record_api_operation(method, url, request_body, 200, None, "")
        return list(s._state["operations"])
    finally:
        s._FEATURE_OPS, s._state["operations"], s.flush = saved_rules, saved_ops, saved_flush


def test_task_types_descends_into_inline_workflowdef():
    assert s._task_types({"name": "wf", "workflowDef": {"tasks": [{"type": "HTTP"},
                                                                  {"type": "INLINE"}]}}) == ["HTTP", "INLINE"]


@_needs_profile
def test_inline_start_records_started_with_task_types():
    ops = _classify("POST", "https://t/api/workflow",
                    {"name": "adhoc", "workflowDef": {"tasks": [{"type": "HTTP"}]}})
    started = [o for o in ops if o["type"] == "workflow_started"]
    assert started and started[0].get("task_types") == ["HTTP"]
    assert started[0].get("workflow_name") == "adhoc"


@_needs_profile
def test_put_bulk_register_records_each_definition():
    ops = _classify("PUT", "https://t/api/metadata/workflow",
                    [{"name": "w1", "tasks": [{"type": "HTTP"}]},
                     {"name": "w2", "tasks": [{"type": "INLINE"}]}])
    regs = [o for o in ops if o["type"] == "workflow_registered"]
    assert len(regs) == 2 and {o["workflow_name"] for o in regs} == {"w1", "w2"}


def test_started_workflow_keeps_its_own_task_types_without_a_register():
    # The exact regression: inline-def runs carry task types even though nothing was registered.
    ops = [{"type": "workflow_started", "workflow_name": "a", "status": 200,
            "task_types": ["EVENT", "HTTP"], "execution_id": "e1"}]
    runs = list(fe._started_workflows(ops).values())
    assert runs[0]["task_types"] == ["EVENT", "HTTP"]


def test_task_types_exercised_counts_started_runs():
    ops = [{"type": "workflow_started", "workflow_name": "a", "status": 200,
            "task_types": ["EVENT", "HTTP"], "execution_id": "e1"}]
    runs = list(fe._started_workflows(ops).values())
    agg = sorted({t for op in runs for t in (op.get("task_types") or [])})
    assert agg == ["EVENT", "HTTP"]
