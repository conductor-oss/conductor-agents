"""Benchmark scoring (spec section 24): FP / FN against ground-truth test apps.

Pure logic so it is unit-testable independent of a live scan. ``score`` compares a
ground-truth list of expected weaknesses against the findings a scan actually
produced and reports false-negative rate (ground truth missed), false-positive rate
(findings with no ground-truth match -- the headline metric on a KNOWN-CLEAN app),
and which expected items matched.
"""

from __future__ import annotations

import re


def _norm(t) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip().lower()


def _matches(expected: dict, finding: dict) -> bool:
    """A finding satisfies an expected weakness if its category/cwe matches OR any of
    the expected keywords appears in the finding's title/category/cwe/location."""
    hay = _norm(" ".join(str(finding.get(k, "")) for k in
                         ("title", "category", "cwe", "owasp", "location", "description")))
    cat = _norm(expected.get("category"))
    if cat and cat in hay:
        return True
    cwe = _norm(expected.get("cwe"))
    if cwe and cwe in hay:
        return True
    for kw in expected.get("keywords") or []:
        if _norm(kw) in hay:
            return True
    return False


def objective_coverage(expected: list, catalog_objectives: list) -> dict:
    """Fraction of catalog objectives that have >=1 benchmark fixture (§19.2 / P3-4).

    An objective is *measured* if a fixture names its ``objective_id`` OR shares its
    ``class``; otherwise it is UNMEASURED — and the self-improvement loop must not auto-tune
    an unmeasured class (it has no ground-truth signal). ``catalog_objectives`` is the
    catalog entry list ({id, class, ...})."""
    fixture_ids = {e.get("objective_id") for e in (expected or []) if e.get("objective_id")}
    fixture_classes = {e.get("class") for e in (expected or []) if e.get("class")}
    measured, unmeasured = [], []
    for obj in catalog_objectives or []:
        oid, cls = obj.get("id"), obj.get("class")
        (measured if (oid in fixture_ids or cls in fixture_classes) else unmeasured).append(oid)
    total = len(catalog_objectives or [])
    return {"total": total, "measured": len(measured),
            "pct": round(len(measured) / total, 3) if total else 0.0,
            "unmeasured": unmeasured}


def score(expected: list, findings: list, catalog_objectives: list | None = None) -> dict:
    """Compare ground truth to actual findings. ``expected`` may be empty (clean app).
    If ``catalog_objectives`` is given, also report benchmark objective-coverage (P3-4)."""
    expected = expected or []
    # Only count substantive findings (ignore explicit false_positive flags).
    real = [f for f in (findings or []) if not f.get("false_positive")]

    # Adversarial corpus (§19.2): positives must be found; NEGATIVES are near-miss traps
    # ("looks like a vuln, isn't") that must NOT be flagged. Recall is over positives only.
    positives = [e for e in expected if e.get("kind") != "negative"]
    negatives = [e for e in expected if e.get("kind") == "negative"]

    matched, missed = [], []
    for exp in positives:
        hit = next((f for f in real if _matches(exp, f)), None)
        (matched if hit else missed).append(exp.get("id") or exp.get("category"))

    # Precision failures: findings that matched a near-miss NEGATIVE (the trap was taken).
    precision_failures = [f.get("title") or f.get("category")
                          for f in real if any(_matches(neg, f) for neg in negatives)]

    # False positives: findings that matched NO positive expectation (headline on a clean app).
    false_positives = [f.get("title") or f.get("category")
                       for f in real if not any(_matches(e, f) for e in positives)]

    n_exp = len(positives)
    fn_rate = (len(missed) / n_exp) if n_exp else 0.0
    fp_rate = (len(false_positives) / len(real)) if real else 0.0
    return {
        "expected": n_exp,
        "found": len(real),
        "matched": matched,
        "missed": missed,
        "false_positives": false_positives,
        "precision_failures": precision_failures,   # near-miss negatives the harness wrongly flagged
        "negatives": len(negatives),
        "fn_rate": round(fn_rate, 3),
        "fp_rate": round(fp_rate, 3),
        "recall": round(1 - fn_rate, 3),
        "by_class": score_by_class(positives, real),
        "objective_coverage": objective_coverage(positives, catalog_objectives) if catalog_objectives else None,
    }


def score_by_class(expected: list, findings: list) -> dict:
    """Per-catalog-class recall (ROADMAP E10): a class isn't credibly 'covered' until the
    benchmark proves the harness detects it. Groups expected weaknesses by their `class`
    and reports detected/total per class."""
    real = [f for f in (findings or []) if not f.get("false_positive")]
    by: dict = {}
    for exp in expected or []:
        cls = exp.get("class") or "uncategorized"
        slot = by.setdefault(cls, {"total": 0, "detected": 0})
        slot["total"] += 1
        if any(_matches(exp, f) for f in real):
            slot["detected"] += 1
    for cls, slot in by.items():
        slot["recall"] = round(slot["detected"] / slot["total"], 3) if slot["total"] else 0.0
    return by
