"""Benchmark scoring (spec 24): FP/FN computation."""
import os
import sys

# bench/ is not on the worker path; add it.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bench"))
import score  # noqa: E402


EXPECTED = [
    {"id": "sqli", "category": "injection", "cwe": "CWE-89", "keywords": ["sql injection", "/search"]},
    {"id": "secret", "category": "hardcoded secret", "cwe": "CWE-798", "keywords": ["aws", "AKIA"]},
]


def test_perfect_recall_no_fp():
    findings = [
        {"title": "SQL injection in /search", "cwe": "CWE-89", "category": "injection"},
        {"title": "Hardcoded AWS access key", "cwe": "CWE-798", "category": "hardcoded secret"},
    ]
    s = score.score(EXPECTED, findings)
    assert s["recall"] == 1.0
    assert s["fn_rate"] == 0.0
    assert s["fp_rate"] == 0.0
    assert s["missed"] == []


def test_false_negative_counted():
    findings = [{"title": "SQL injection in /search", "cwe": "CWE-89"}]
    s = score.score(EXPECTED, findings)
    assert "secret" in s["missed"]
    assert s["fn_rate"] == 0.5
    assert s["recall"] == 0.5


def test_false_positive_on_clean_app():
    # No ground truth (clean app); any finding is a false positive.
    findings = [{"title": "Some noisy nuclei hit", "category": "info"}]
    s = score.score([], findings)
    assert s["fp_rate"] == 1.0
    assert len(s["false_positives"]) == 1


def test_clean_app_clean_scan_is_perfect():
    s = score.score([], [])
    assert s["fp_rate"] == 0.0 and s["fn_rate"] == 0.0


def test_per_class_recall():
    expected = [
        {"id": "a", "class": "infra", "cwe": "CWE-89", "keywords": ["sql injection"]},
        {"id": "b", "class": "infra", "cwe": "CWE-78", "keywords": ["command injection"]},
        {"id": "c", "class": "authz", "cwe": "CWE-862", "keywords": ["missing auth"]},
    ]
    findings = [{"title": "SQL injection", "cwe": "CWE-89"}, {"title": "missing auth on admin"}]
    by = score.score_by_class(expected, findings)
    assert by["infra"]["total"] == 2 and by["infra"]["detected"] == 1 and by["infra"]["recall"] == 0.5
    assert by["authz"]["detected"] == 1 and by["authz"]["recall"] == 1.0


def test_false_positive_flag_ignored():
    findings = [
        {"title": "SQL injection in /search", "cwe": "CWE-89"},
        {"title": "Missing header (benign)", "false_positive": True},
    ]
    s = score.score(EXPECTED, findings)
    # the false_positive-flagged finding is excluded from both found and FP counts
    assert s["found"] == 1
    assert s["false_positives"] == []
