"""E9 — compliance rollup + regression export/retest scoring."""
from common import compliance, regression


def _ledger():
    return [
        {"objective_id": "A", "class": "authz", "status": "tested", "refs": {"owasp": "A01:2021", "asvs": "V4.1", "cwe": "CWE-639"}},
        {"objective_id": "B", "class": "authz", "status": "untested", "refs": {"owasp": "A01:2021", "asvs": "V4.2", "cwe": "CWE-200"}},
        {"objective_id": "C", "class": "infra", "status": "blocked", "refs": {"owasp": "A10:2021", "asvs": "V12.6", "cwe": "CWE-918"}},
        {"objective_id": "D", "class": "client", "status": "not_applicable", "refs": {"owasp": "A03:2021", "asvs": "V5.3", "cwe": "CWE-79"}},
    ]


def test_rollup_counts_and_frameworks():
    r = compliance.rollup(_ledger())
    o = r["objectives"]
    assert o["applicable"] == 3            # tested + untested + blocked (NOT not_applicable)
    assert o["tested"] == 1 and o["blocked"] == 1 and o["not_applicable"] == 1
    assert r["owasp"]["A01"]["applicable"] == 2 and r["owasp"]["A01"]["tested"] == 1
    assert "V4" in r["asvs"] and "V12" in r["asvs"]
    assert r["posture"] in ("shallow", "partial", "strong")


def test_regression_bundle_only_with_poc():
    confirmed = [
        {"title": "real", "objective_id": "X", "severity": "High", "poc_request": {"method": "GET", "url": "/x"}},
        {"title": "no poc", "objective_id": "Y", "poc_request": {}},
    ]
    b = regression.bundle(confirmed)
    assert len(b) == 1 and b[0]["title"] == "real" and b[0]["poc_request"]["url"] == "/x"


def test_score_retest_fixed_vs_still():
    items = [{"id": "R-1", "title": "a"}, {"id": "R-2", "title": "b"}, {"id": "R-3", "title": "c"}]
    replays = {"R-1": {"reproduced": False}, "R-2": {"reproduced": True}}  # R-3 missing -> unknown
    s = regression.score_retest(items, replays)
    assert s["fixed"] == 1 and s["still_vulnerable"] == 1 and s["unknown"] == 1
