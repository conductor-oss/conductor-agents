"""Evidence-state ladder + dual live/source rating (PLAN_V3 section 5.4, item #2)."""
import json
from pathlib import Path

from common import evidence_state as es


def test_zero_confirmed_source_findings_are_source_candidate_not_live():
    """This run's exact failure: 4 source 'High's, 0 runtime-confirmed. Live rating must be None."""
    triage = {"risk_rating": "High", "findings": [
        {"id": "F-1", "title": "Possible SQL injection via formatted SQL", "severity": "High"},
        {"id": "F-2", "title": "Default JWT signing secret committed", "severity": "High"},
        {"id": "F-6", "title": "Containers run as root", "severity": "Medium"},
    ]}
    res = es.annotate(triage, confirmed=[], surface=[])
    assert res["live_risk_rating"] == es.NONE_RATING
    assert res["source_candidate_rating"] == "High"
    assert res["confirmed_count"] == 0
    assert all(f["evidence_state"] == es.SOURCE_ONLY for f in res["triage"]["findings"])
    assert res["evidence_state_counts"][es.SOURCE_ONLY] == 3


def test_oob_confirmed_finding_drives_live_rating_and_tag():
    triage = {"findings": [
        {"id": "F-1", "title": "SSRF via HTTP task to internal metadata", "severity": "High"},
        {"id": "F-2", "title": "Default JWT secret committed", "severity": "High"},
    ]}
    confirmed = [{"title": "SSRF via HTTP task to internal metadata",
                  "severity": "High", "source_tool": "oob_confirmed"}]
    res = es.annotate(triage, confirmed=confirmed)
    tags = {f["id"]: f["evidence_state"] for f in res["triage"]["findings"]}
    assert tags["F-1"] == es.RUNTIME_OOB_CONFIRMED
    assert tags["F-2"] == es.SOURCE_ONLY
    assert res["live_risk_rating"] == "High"
    assert res["oob_confirmed_count"] == 1
    assert res["source_candidate_rating"] == "High"  # F-2 remains a source High


def test_deep_exploit_finding_is_runtime_reproduced():
    triage = {"findings": [{"id": "F-1", "title": "Reflected XSS in search parameter",
                            "severity": "Medium"}]}
    confirmed = [{"title": "Reflected XSS in search parameter", "severity": "Medium",
                  "source_tool": "deep_exploit"}]
    res = es.annotate(triage, confirmed=confirmed)
    assert res["triage"]["findings"][0]["evidence_state"] == es.RUNTIME_REPRODUCED
    assert res["live_risk_rating"] == "Medium"


def test_false_positive_not_classified():
    triage = {"findings": [{"id": "F-1", "title": "benign", "severity": "High",
                            "false_positive": True}]}
    res = es.annotate(triage, confirmed=[])
    assert "evidence_state" not in res["triage"]["findings"][0]
    assert res["source_candidate_rating"] == es.NONE_RATING
    assert all(v == 0 for v in res["evidence_state_counts"].values())


def test_live_observed_surface_finding_is_source_attested():
    triage = {"findings": [{"id": "F-1", "title": "Reflected XSS on profile page",
                            "severity": "Medium"}]}
    surface = [{"title": "Reflected XSS on profile page", "severity": "Medium",
                "provenance": "observed"}]
    res = es.annotate(triage, confirmed=[], surface=surface)
    assert res["triage"]["findings"][0]["evidence_state"] == es.SOURCE_ATTESTED
    # source_attested is NOT a runtime rung -> does not drive the live rating
    assert res["live_risk_rating"] == es.NONE_RATING
    assert res["source_candidate_rating"] == "Medium"


def test_live_rating_comes_from_confirmed_set_not_title_join():
    """The headline live rating must not depend on the triage LLM's wording: even when the
    reworded triage row does not match, the confirmed set still sets the live rating, and the
    unmatched row is never inflated to a runtime rung."""
    triage = {"findings": [{"id": "F-1", "title": "completely different wording here",
                            "severity": "Low"}]}
    confirmed = [{"title": "IPv6 loopback SSRF retrieves internal spec", "severity": "High",
                  "source_tool": "oob_confirmed"}]
    res = es.annotate(triage, confirmed=confirmed)
    assert res["live_risk_rating"] == "High"
    assert res["confirmed_count"] == 1 and res["oob_confirmed_count"] == 1
    assert res["triage"]["findings"][0]["evidence_state"] == es.SOURCE_ONLY


def test_no_findings_yields_none():
    res = es.annotate({"findings": []}, confirmed=[])
    assert res["live_risk_rating"] == es.NONE_RATING
    assert res["source_candidate_rating"] == es.NONE_RATING


def test_evidence_state_wired_before_prep_report():
    wf = json.loads((Path(__file__).resolve().parents[1] / "conductor" / "workflows"
                     / "deep_assess.json").read_text(encoding="utf-8"))
    tasks = {t["taskReferenceName"]: t for t in wf["tasks"]}
    assert "evidence_state" in tasks
    assert tasks["evidence_state"]["name"] == "classify_evidence_state"
    pr = tasks["prep_report"]["inputParameters"]
    assert pr["triage"] == "${evidence_state.output.triage}"
    assert pr["live_risk_rating"] == "${evidence_state.output.live_risk_rating}"
    out = wf["outputParameters"]
    assert out["live_risk_rating"] == "${evidence_state.output.live_risk_rating}"
    assert out["source_candidate_rating"] == "${evidence_state.output.source_candidate_rating}"
