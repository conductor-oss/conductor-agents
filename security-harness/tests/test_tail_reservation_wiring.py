"""Tail-reservation pass-loop wiring (item #7): feature_triage runs per-pass with reserved_ids,
the reservation is computed at the loop tail, and no post-loop task is left referencing the
now-in-loop feature_triage (the cross-scope __i trap the plan warned about)."""
import json
import re
from pathlib import Path

WF = json.loads((Path(__file__).resolve().parents[1] / "conductor" / "workflows"
                 / "deep_assess.json").read_text(encoding="utf-8"))
_TOP = WF["tasks"]
_LOOP = next(t for t in _TOP if t.get("type") == "DO_WHILE")
_LOOP_REFS = {t["taskReferenceName"] for t in _LOOP["loopOver"]}
_TOP_REFS = {t["taskReferenceName"] for t in _TOP}


def _refs(task):
    return set(re.findall(r"\$\{([a-zA-Z0-9_]+)\.output", json.dumps(task.get("inputParameters", {}))))


def test_feature_triage_moved_into_loop():
    assert "feature_triage" in _LOOP_REFS
    assert "feature_triage" not in _TOP_REFS  # no longer pre-loop


def test_feature_triage_consumes_reserved_ids():
    ft = next(t for t in _LOOP["loopOver"] if t["taskReferenceName"] == "feature_triage")
    assert ft["inputParameters"]["reserved_ids"] == "${workflow.variables.reserved_ids}"


def test_reservation_tasks_wired_at_loop_tail():
    order = [t["taskReferenceName"] for t in _LOOP["loopOver"]]
    for name in ("build_feature_graph_pass", "schedule_feature_campaign", "merge_reserved", "set_reserved"):
        assert name in order, f"{name} missing from loop"
    # feature_triage precedes assess_pass; the reservation follows set_state.
    assert order.index("feature_triage") < order.index("assess_pass")
    assert order.index("set_state") < order.index("schedule_feature_campaign")


def test_reserved_ids_seeded_and_written_back():
    seed = next(t for t in _TOP if t["taskReferenceName"] == "seed_state")
    assert seed["inputParameters"].get("reserved_ids") == []
    sr = next(t for t in _LOOP["loopOver"] if t["taskReferenceName"] == "set_reserved")
    assert set(sr["inputParameters"]) >= {"reserved_ids", "probed_features", "triage_signals_v"}
    assert sr["inputParameters"]["reserved_ids"] == "${merge_reserved.output.result}"


def test_no_post_loop_task_references_in_loop_feature_triage():
    # The exact cross-scope trap: post-loop consumers must read workflow.variables, not the
    # now-in-loop feature_triage output.
    offenders = [t["taskReferenceName"] for t in _TOP
                 if t["taskReferenceName"] != "pass_loop" and "feature_triage" in _refs(t)]
    assert offenders == []
