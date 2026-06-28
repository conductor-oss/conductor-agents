"""Benchmark objective-coverage metric (§19.2 / P3-4): unmeasured classes must be flagged."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bench"))
import score  # noqa: E402


def test_objective_coverage_flags_unmeasured():
    catalog = [{"id": "A", "class": "x"}, {"id": "B", "class": "y"}, {"id": "C", "class": "z"}]
    # B is covered by objective_id; A by its class; C has no fixture -> unmeasured.
    expected = [{"objective_id": "B", "class": "y"}, {"class": "x"}]
    cov = score.objective_coverage(expected, catalog)
    assert cov["total"] == 3 and cov["measured"] == 2 and cov["pct"] == round(2 / 3, 3)
    assert cov["unmeasured"] == ["C"]


def test_score_includes_coverage_only_when_catalog_given():
    catalog = [{"id": "A", "class": "x"}, {"id": "B", "class": "y"}]
    expected = [{"id": "e1", "class": "x", "keywords": ["sqli"]}]
    findings = [{"title": "sqli in login", "category": "injection"}]
    s = score.score(expected, findings, catalog)
    assert s["objective_coverage"]["measured"] == 1
    assert s["objective_coverage"]["unmeasured"] == ["B"]
    # backward-compatible: no catalog -> coverage omitted (None)
    assert score.score(expected, findings)["objective_coverage"] is None
