"""Living dossier + attack graph + finding lifecycle (spec 19/20/26)."""
from common import dossier


def test_attack_graph_edges_from_chain():
    confirmed = [
        {"title": "User account identifier disclosure", "category": "info_exposure",
         "evidence": "response leaks the userid and reset token for any account", "severity": "Medium"},
        {"title": "Account takeover via userid reset token", "category": "auth",
         "evidence": "reset accepts an arbitrary userid", "severity": "High"},
    ]
    g = dossier.build_attack_graph(confirmed)
    assert len(g["nodes"]) == 2
    # the info_exposure finding shares >=2 tokens (account/userid/reset/token) -> may-enable
    assert any(e["from"] == "F0" and e["to"] == "F1" for e in g["edges"])


def test_attack_graph_no_spurious_edges():
    confirmed = [
        {"title": "Clickjacking on marketing page", "category": "misconfig", "evidence": ""},
        {"title": "Verbose error on 500", "category": "info_exposure", "evidence": "stack trace"},
    ]
    g = dossier.build_attack_graph(confirmed)
    assert g["edges"] == []  # unrelated, non-overlapping


def test_residual_risk_mentions_untested_and_blind():
    rr = dossier.residual_risk(
        confirmed=[{"severity": "High", "title": "x"}],
        coverage_summary={"untested_keys": ["invariant:single-use coupon"],
                          "by_status": {"untested": 1, "tested": 3}},
        blind=[{"title": "blind ssrf lead"}], contradictions=[])
    assert "NOT tested" in rr or "not tested" in rr.lower()
    assert "blind" in rr.lower()
    assert "high/critical" in rr.lower()


def test_residual_risk_no_findings_still_honest():
    rr = dossier.residual_risk([], {}, [], [])
    assert "not overall security" in rr.lower() or "only what was tested" in rr.lower()


def test_build_assembles_all_sections():
    doc = dossier.build(
        authorization={"reason": "authorized"}, fingerprint="fp1",
        app_model={"purpose": "p", "trust_boundaries": ["db"]},
        personas=[{"label": "anon"}], documented_invariants=[{"invariant": "coupon single-use"}],
        coverage_summary={"by_status": {"tested": 1}}, confirmed=[{"title": "f", "category": "bola"}],
        rejected=[], blind=[], contradictions=[])
    for key in ("authorization", "application_model", "personas", "invariant_catalog",
                "coverage", "confirmed_findings", "attack_graph", "residual_risk"):
        assert key in doc


def test_lifecycle_states_defined():
    assert "confirmed" in dossier.LIFECYCLE
    assert "regression_verified" in dossier.LIFECYCLE
    assert "stale" in dossier.LIFECYCLE


def test_residual_risk_loudly_flags_zero_attempts():
    """A run that exercised nothing (refusal / empty hypotheses -> 0 objective_attempts) must NOT
    read as a clean bill of health — residual risk leads with a loud warning."""
    rr = dossier.residual_risk([], {"by_status": {}}, [], [], {}, attempts=0)
    assert "NO TEST HYPOTHESES WERE EXERCISED" in rr
    rr2 = dossier.residual_risk([], {"by_status": {"tested": 3}}, [], [], {}, attempts=5)
    assert "NO TEST HYPOTHESES" not in rr2


def test_build_derives_zero_attempts_from_ledger():
    doc = dossier.build(
        authorization={"reason": "authorized"}, fingerprint="fp",
        app_model={"purpose": "p"}, personas=[], documented_invariants=[],
        coverage_summary={}, confirmed=[], rejected=[], blind=[], contradictions=[],
        operation_ledger=[{"type": "http_request"}])     # no objective_attempt -> 0 attempts
    assert "NO TEST HYPOTHESES WERE EXERCISED" in doc["residual_risk"]
