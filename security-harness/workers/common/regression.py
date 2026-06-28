"""Regression-suite export + retest scoring (ROADMAP E9).

Each confirmed finding with a re-runnable proof becomes a regression test: a
``{id, title, objective_id, severity, poc_request, expects}`` record. ``score_retest``
compares a fresh re-run of those PoCs against the bundle to report which findings are
now FIXED vs STILL VULNERABLE — the basis for `--retest` remediation verification.
Pure logic, unit-testable.
"""

from __future__ import annotations


def bundle(confirmed: list) -> list[dict]:
    """Build the regression suite from confirmed findings that carry a PoC request."""
    out = []
    for i, f in enumerate(confirmed or []):
        poc = f.get("poc_request") or {}
        if not (isinstance(poc, dict) and poc.get("url")):
            continue
        out.append({
            "id": f.get("hypothesis_id") or f.get("objective_id") or f"R-{i+1}",
            "title": f.get("title"),
            "objective_id": f.get("objective_id"),
            "severity": f.get("severity"),
            "poc_request": {"method": poc.get("method", "GET"), "url": poc.get("url"),
                            "identity": poc.get("identity"), "json": poc.get("json")},
            "expects": "vulnerable: the PoC reproduces the impact (finding still present)",
        })
    return out


def score_retest(bundle_items: list, replays: dict) -> dict:
    """Compare a fresh replay of each bundle item against expectation.

    ``replays`` maps test id -> {reproduced: bool} (reproduced=True means the PoC STILL
    works -> still vulnerable; False -> fixed). Returns a per-item verdict + summary."""
    items, results = bundle_items or [], []
    fixed = still = unknown = 0
    for it in items:
        rep = (replays or {}).get(it["id"])
        if rep is None:
            verdict, unknown = "unknown", unknown + 1
        elif rep.get("reproduced"):
            verdict, still = "still_vulnerable", still + 1
        else:
            verdict, fixed = "fixed", fixed + 1
        results.append({"id": it["id"], "title": it.get("title"), "verdict": verdict})
    return {"total": len(items), "fixed": fixed, "still_vulnerable": still, "unknown": unknown,
            "results": results,
            "summary": f"retest: {fixed} fixed, {still} still vulnerable, {unknown} unknown of {len(items)}"}
