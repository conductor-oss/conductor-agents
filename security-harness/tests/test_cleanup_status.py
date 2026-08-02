"""Final cleanup status CLEAN / RETAINED / UNRESOLVED (PLAN_V3 Phase 5.5)."""
from common import cleanup_status as cs


def test_clean_when_no_residue_no_retention():
    assert cs.finalize({"deleted": ["a", "b"], "residue": [], "retained": []}) == cs.CLEAN


def test_retained_when_leave_evidence():
    assert cs.finalize({"deleted": [], "residue": [], "leave_evidence": True,
                        "retained": ["x"]}) == cs.RETAINED


def test_unresolved_wins_over_retention():
    # residue means something could not be removed -> UNRESOLVED regardless of intent
    assert cs.finalize({"deleted": ["a"], "residue": ["b"], "leave_evidence": True}) == cs.UNRESOLVED
    assert cs.finalize({"residue": ["b"]}) == cs.UNRESOLVED


def test_summarize_attaches_status_and_line():
    out = cs.summarize({"deleted": ["a"], "residue": []})
    assert out["status"] == cs.CLEAN and "CLEAN" in out["status_summary"]
    out2 = cs.summarize({"residue": ["x", "y"]})
    assert out2["status"] == cs.UNRESOLVED and "could not be removed" in out2["status_summary"]


def test_finalize_defensive_on_junk():
    assert cs.finalize(None) == cs.CLEAN
    assert cs.finalize("nope") == cs.CLEAN
