"""Machine-checked invariants from docs/EXPLOIT_DEEPENING_VERIFICATION.md.

Structural (no server): I1 completion-criterion invariance, I2 evidence-bar invariance, I3
termination preservation (every loop hard-bounded), I5 dedup keeps families (all four reducers),
I6 governance/surface. A failing assertion here means an edit left the property-preserving class of
the Conservative-Extension Theorem (§2) and is a release blocker. (I4 well-formedness lives in
test_exploit_deepening; I7 is the runtime validator bench/verify_deepening.py.)"""
import json
import os

from conftest import REPO
from common import hillclimb


def _wf(name):
    return json.load(open(os.path.join(REPO, "conductor", "workflows", name)))


def _walk(tasks, fn):
    for t in tasks:
        fn(t)
        for key in ("loopOver", "forkTasks", "tasks"):
            v = t.get(key)
            if isinstance(v, list):
                [_walk(s, fn) for s in v] if (v and isinstance(v[0], list)) else _walk(v, fn)
        for b in (t.get("decisionCases") or {}).values():
            _walk(b, fn)


# I1 — the completion expression is byte-identical to the recorded baseline, and no deepening
# coverage value is wired into the completion path at the workflow-def level.
EXPECTED_PASS_LOOP_COND = "(function(){ return $.pass_loop['iteration'] < $.max_passes && $.keep_going === true; })();"


def test_I1_completion_expression_byte_identical():
    d = _wf("deep_assess.json")
    pass_loop = next(t for t in d["tasks"] if t.get("taskReferenceName") == "pass_loop")
    assert pass_loop["loopCondition"] == EXPECTED_PASS_LOOP_COND
    # technique_coverage rides INSIDE feature_exercise at runtime; it must not be a new literal
    # wired into deep_assess's control flow (the completion path is unchanged).
    assert "technique_coverage" not in json.dumps(d)


def test_I2_evidence_bar_inputs_unchanged():
    v = _wf("verify_finding.json")
    assert v["inputParameters"] == ["finding", "identities", "scope"]
    blob = json.dumps(v)
    assert "family" not in blob and "technique_coverage" not in blob


def test_I3_every_loop_is_hard_bounded():
    # P1 adds no loop/fork; the existing termination guarantee rests on each DO_WHILE carrying a
    # constant `iteration < ...` upper bound. Assert that property holds for every loop.
    for name in ("deep_assess.json", "assess_pass.json", "exploit_agent.json", "explore_agent.json"):
        loops = []
        _walk(_wf(name)["tasks"], lambda t: loops.append(t) if t.get("type") == "DO_WHILE" else None)
        for lp in loops:
            cond = lp.get("loopCondition", "")
            assert "iteration" in cond and "<" in cond, \
                f"{name}::{lp.get('taskReferenceName')} loop lacks a hard iteration bound: {cond}"
    # the pass loop's hard bound is specifically max_passes
    assert "$.max_passes" in EXPECTED_PASS_LOOP_COND


def test_I5_family_in_all_four_reducers():
    targets = [("assess_pass.json", "collect_operations"), ("deep_assess.json", "accumulate"),
               ("exploit_agent.json", "build_turn_code"), ("exploit_agent.json", "build_turn")]
    for wf, ref in targets:
        spans = []
        _walk(_wf(wf)["tasks"],
              lambda t: spans.append((t.get("inputParameters") or {}).get("queryExpression", ""))
              if t.get("taskReferenceName") == ref else None)
        qe = next((s for s in spans if "unique_by" in s), "")
        i = qe.find("unique_by(")
        assert i >= 0 and "(.family" in qe[i:], f"{wf}::{ref} unique_by drops family"


def test_I6_no_new_auto_tune_surface_and_no_rejected_engine():
    # Exploit-deepening added no AUTO-tune surface. The HC-superpower work intentionally adds the
    # `tradecraft` surface, but RATIFY-only (human-gated detection machinery) — so the AUTO-tunable
    # set is unchanged, and safety stays never-tunable.
    assert {s for s, m in hillclimb.SURFACE_MODE.items() if m == "auto"} == {"profile", "prompt"}
    assert hillclimb.SURFACE_MODE.get("tradecraft") == "ratify"
    assert hillclimb.SURFACE_MODE.get("safety") == "never"
    # The rejected per-feature "techniques engine" (EXPLOIT_DEEPENING.md §5) was NOT introduced.
    assert not os.path.exists(os.path.join(REPO, "workers", "common", "techniques.py"))
