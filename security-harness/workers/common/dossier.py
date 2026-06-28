"""Living security dossier + attack graph (spec sections 19, 20, 26).

The campaign deliverable is a living dossier, not just a findings list. This module
assembles one from the artifacts the run already produced (authorization record,
fingerprint, app/trust model, personas, invariant catalog, coverage ledger,
confirmed/rejected/blind hypotheses, attack graph, residual-risk statement) and
derives a simple attack graph from confirmed findings + their chaining relationships.

Pure logic, unit-testable.
"""

from __future__ import annotations

import re

# Lifecycle states (spec 19). Transitions are computed in memory.merge_run across runs.
LIFECYCLE = ["hypothesis", "planned", "authorized", "tested", "anomalous",
             "reproduced", "confirmed", "rejected", "inconclusive",
             "remediated", "regression_verified", "stale"]


def _toks(t) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", str(t or "").lower()) if len(w) >= 4}


def build_attack_graph(confirmed: list, hypotheses: list | None = None) -> dict:
    """Derive nodes + edges. Nodes are confirmed findings; an edge A->B is drawn when
    A is a chaining/info-disclosure/auth weakness whose tokens overlap B's (A plausibly
    enables B), modelling second-order impact (spec 11.11)."""
    confirmed = confirmed or []
    nodes = [{"id": f"F{i}", "title": f.get("title"), "severity": f.get("severity"),
              "category": f.get("category"), "lifecycle": f.get("lifecycle", "confirmed")}
             for i, f in enumerate(confirmed)]
    enabling = {"info_exposure", "info_disclosure", "idor", "bola", "auth", "chaining", "ssrf"}
    edges = []
    for i, a in enumerate(confirmed):
        if str(a.get("category") or "").lower() not in enabling:
            continue
        atoks = _toks(a.get("title")) | _toks(a.get("evidence"))
        for j, b in enumerate(confirmed):
            if i == j:
                continue
            if len(atoks & (_toks(b.get("title")))) >= 2:
                edges.append({"from": f"F{i}", "to": f"F{j}",
                              "rel": "may-enable", "via": a.get("category")})
    return {"nodes": nodes, "edges": edges}


def residual_risk(confirmed: list, coverage_summary: dict | None, blind: list | None,
                  contradictions: list | None, feature_exercise: dict | None = None,
                  attempts: int | None = None) -> str:
    """One-paragraph residual-risk statement (spec 26): what remains unknown / untested.

    ``attempts`` = number of objective exploit attempts actually made. When it is 0 and nothing was
    found, the assessment did NOT exercise the app (a model refusal or empty hypothesis generation)
    — that must be stated LOUDLY so an empty run is never mistaken for a clean bill of health."""
    coverage_summary = coverage_summary or {}
    untested = coverage_summary.get("untested_keys") or []
    by_status = coverage_summary.get("by_status") or {}
    parts = []
    if attempts == 0 and not (confirmed or blind):
        parts.append(
            "⚠ NO TEST HYPOTHESES WERE EXERCISED (0 attempts): hypothesis generation produced "
            "nothing or the model DECLINED (possible refusal), so the attack surface was NOT actively "
            "tested. This is NOT a security assessment and absence of findings means nothing — re-run "
            "(e.g. with a different model) before relying on it.")
    crit = [f for f in (confirmed or []) if str(f.get("severity")).lower() in ("critical", "high")]
    if crit:
        parts.append(f"{len(crit)} high/critical issue(s) confirmed and should be remediated first.")
    if untested:
        parts.append(f"{len(untested)} coverage cell(s) were NOT tested "
                     f"({by_status.get('untested', len(untested))} untested vs "
                     f"{by_status.get('tested', 0)} tested) -- absence of findings there is NOT assurance.")
    if blind:
        parts.append(f"{len(blind)} blind/out-of-band lead(s) remain unconfirmed and need manual verification.")
    if contradictions:
        parts.append(f"{len(contradictions)} documented-vs-observed contradiction(s) are unresolved.")
    pending = (feature_exercise or {}).get("pending") or []
    if pending:
        parts.append(
            "Mandatory product-feature/CVE exercises remain incomplete: "
            + ", ".join(str(x) for x in pending)
            + "."
        )
    if not parts:
        parts.append("No confirmed findings; note that this reflects only what was tested, not overall security.")
    return " ".join(parts)


def build(*, authorization: dict, fingerprint: str, app_model: dict, personas: list,
          documented_invariants: list, coverage_summary: dict, confirmed: list,
          rejected: list, blind: list, contradictions: list,
          cleanup: dict | None = None, coverage_ledger: list | None = None,
          attack_graph: dict | None = None, operation_ledger: list | None = None,
          feature_exercise: dict | None = None, feeds_as_of: str | None = None) -> dict:
    """Assemble the spec-26 living dossier from the run's artifacts, incl. the E9
    compliance rollup (OWASP/ASVS) and the re-runnable regression bundle."""
    from common import compliance as compliance_mod
    from common import regression as regression_mod
    return {
        "authorization": authorization or {},
        "fingerprint": fingerprint,
        "application_model": {"purpose": (app_model or {}).get("purpose"),
                              "trust_boundaries": (app_model or {}).get("trust_boundaries", [])},
        "personas": personas or [],
        "invariant_catalog": documented_invariants or [],
        "coverage": coverage_summary or {},
        "compliance": compliance_mod.rollup(coverage_ledger or []),
        "confirmed_findings": confirmed or [],
        "rejected_findings": rejected or [],
        "blind_leads": blind or [],
        "contradictions": contradictions or [],
        "attack_graph": attack_graph or build_attack_graph(confirmed),
        "operation_ledger": operation_ledger or [],
        "feature_exercise": feature_exercise or {},
        "feeds_as_of": feeds_as_of or "",
        "regression": regression_mod.bundle(confirmed),
        "cleanup": cleanup or {},
        "residual_risk": residual_risk(
            confirmed, coverage_summary, blind, contradictions, feature_exercise,
            attempts=sum(1 for o in (operation_ledger or [])
                         if (o.get("type") or o.get("kind")) == "objective_attempt"),
        ),
    }
