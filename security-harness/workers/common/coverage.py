"""Multi-dimensional coverage ledger (spec section 22).

"Visited every URL" is not security coverage, and no single percentage may stand in
for it. This module enumerates coverage CELLS across the spec's security-relevant
dimensions -- persona, documented invariant, sensitive operation, object-id pattern
(BOLA/IDOR), and interface -- then classifies each as tested / partial / untested /
inconclusive from a run's confirmed/rejected findings and tried-hypothesis signatures.

The classification is heuristic (string association between a cell and the findings/
signatures that reference it); the value is making blind spots explicit so the reflect
critic and the report can name what was NOT covered, rather than implying completeness.
Pure logic, unit-testable.
"""

from __future__ import annotations

import re

TESTED = "tested"
PARTIAL = "partial"
UNTESTED = "untested"
INCONCLUSIVE = "inconclusive"
NOT_APPLICABLE = "not_applicable"
BLOCKED = "blocked"


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _tokens(text) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", _norm(text)) if len(t) >= 3}


def build_cells(app_model: dict | None, personas: list | None,
                docs_digest: dict | None) -> list[dict]:
    """Enumerate coverage cells across the dimensions we have data for."""
    app_model = app_model or {}
    docs_digest = docs_digest or {}
    cells: list[dict] = []

    def add(dimension, key, extra=""):
        key = _norm(key)
        if key:
            cells.append({"dimension": dimension, "key": key, "context": extra})

    for p in (personas or []):
        add("persona", p.get("persona") or p.get("label"), p.get("label"))
    for inv in (docs_digest.get("documented_invariants") or []):
        add("invariant", (inv.get("invariant") if isinstance(inv, dict) else inv), "documented")
    for op in (app_model.get("sensitive_operations") or []):
        add("sensitive_operation", op)
    for oid in (app_model.get("object_id_patterns") or []):
        add("object_id", oid)
    for tb in (app_model.get("trust_boundaries") or []):
        add("trust_boundary", tb)
    # Interfaces are coarse but always present.
    for iface in ("http_api", "browser_ui"):
        add("interface", iface)
    return cells


def _references(cell: dict, texts: list[str]) -> bool:
    """True iff any text shares enough tokens with the cell key to be 'about' it."""
    ck = _tokens(cell["key"]) | _tokens(cell.get("context"))
    if not ck:
        return False
    for t in texts:
        tt = _tokens(t)
        # Strong association: the cell's distinctive tokens substantially appear.
        overlap = ck & tt
        if overlap and (len(overlap) >= 2 or len(ck) == 1 and overlap):
            return True
    return False


def classify(cells: list[dict], confirmed: list | None, tried: list | None,
             rejected: list | None) -> list[dict]:
    """Mark each cell tested / partial / untested / inconclusive."""
    confirmed_txt = [f"{f.get('title','')} {f.get('category','')} {f.get('owasp','')}"
                     for f in (confirmed or [])]
    rejected_txt = [f"{f.get('title','')}" for f in (rejected or [])]
    tried_txt = list(tried or [])

    ledger = []
    for cell in cells:
        if _references(cell, confirmed_txt):
            status = TESTED
        elif _references(cell, rejected_txt):
            status = TESTED  # we did test it -- the claim was disproven
        elif _references(cell, tried_txt):
            status = PARTIAL  # a hypothesis targeted it but nothing was confirmed/rejected
        else:
            status = UNTESTED
        ledger.append({**cell, "status": status})
    return ledger


def summary(ledger: list[dict]) -> dict:
    """Counts per dimension and per status (no single overall percentage, per spec 22)."""
    by_status: dict[str, int] = {}
    by_dimension: dict[str, dict] = {}
    for cell in ledger:
        s = cell["status"]
        by_status[s] = by_status.get(s, 0) + 1
        d = by_dimension.setdefault(cell["dimension"], {})
        d[s] = d.get(s, 0) + 1
    untested = [c for c in ledger if c["status"] == UNTESTED]
    return {
        "total_cells": len(ledger),
        "by_status": by_status,
        "by_dimension": by_dimension,
        "untested_keys": [f"{c['dimension']}:{c['key']}" for c in untested][:50],
    }


def build(app_model, personas, docs_digest, confirmed, tried, rejected) -> dict:
    """Convenience one-shot: cells -> classified ledger + summary."""
    cells = build_cells(app_model, personas, docs_digest)
    ledger = classify(cells, confirmed, tried, rejected)
    return {"ledger": ledger, "summary": summary(ledger)}


def _obj_text(entry: dict) -> str:
    return " ".join(str(entry.get(k, "")) for k in ("objective", "class", "coverage_dimension"))


def build_from_catalog(applicable: list, not_applicable: list,
                       confirmed: list, tried: list, rejected: list,
                       adequacy: dict | None = None) -> dict:
    """Catalog-driven coverage (ROADMAP E0/E1): one cell per APPLICABLE security objective,
    classified tested/partial/untested; NON-applicable objectives are recorded as
    not_applicable; objectives that need identities the campaign lacks (e.g. cross-tenant
    with one credential) are **blocked** rather than falsely untested/clean. Findings match
    an objective by explicit ``objective_id`` (preferred) or token overlap."""
    from common import identity as identity_mod  # local import: avoid cycle at load
    confirmed, rejected, tried = confirmed or [], rejected or [], tried or []
    done_ids = {f.get("objective_id") for f in (confirmed + rejected) if f.get("objective_id")}
    done_txt = [f"{f.get('title','')} {f.get('category','')} {f.get('owasp','')}" for f in (confirmed + rejected)]
    tried_txt = list(tried)

    ledger = []
    for e in (applicable or []):
        oid = e.get("id")
        cell = {"objective_id": oid, "key": oid, "class": e.get("class"),
                "dimension": e.get("coverage_dimension"), "objective": e.get("objective"),
                "refs": e.get("refs", {})}
        probe = {"key": _obj_text(e), "context": oid or ""}
        if (oid and oid in done_ids) or _references(probe, done_txt):
            cell["status"] = TESTED          # actually exercised -> always wins
        elif identity_mod.blocked_by_adequacy(e.get("required_identities"), adequacy):
            cell["status"] = BLOCKED         # can't be proven with the supplied identities
            cell["blocked_reason"] = "insufficient identities: " + (e.get("required_identities") or "more identities")
        elif (oid and oid in {t for t in tried_txt}) or _references(probe, tried_txt):
            cell["status"] = PARTIAL
        else:
            cell["status"] = UNTESTED
        ledger.append(cell)
    for e in (not_applicable or []):
        ledger.append({"objective_id": e.get("id"), "key": e.get("id"), "class": e.get("class"),
                       "dimension": e.get("coverage_dimension"), "status": NOT_APPLICABLE})
    return {"ledger": ledger, "summary": summary(ledger)}
