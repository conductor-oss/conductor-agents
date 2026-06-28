"""Compliance / assurance rollup (ROADMAP E9).

Turns the catalog-driven coverage ledger into the framework-mapped assurance view a
paying customer expects: per-OWASP-2021 and per-ASVS-chapter coverage, and an overall
objective posture (applicable / tested / partial / blocked / untested). It reads the
``refs`` and ``status`` already on each coverage cell (from the catalog), so it stays in
sync with the catalog automatically. Pure logic, unit-testable.
"""

from __future__ import annotations

import re

_COUNTED_TESTED = {"tested"}
_APPLICABLE_STATUSES = {"tested", "partial", "untested", "blocked"}  # excludes not_applicable


def _asvs_chapter(asvs: str) -> str:
    m = re.search(r"V?(\d+)", str(asvs or ""))
    return f"V{m.group(1)}" if m else ""


def _bump(d: dict, key: str, status: str):
    if not key:
        return
    cell = d.setdefault(key, {"applicable": 0, "tested": 0, "partial": 0, "blocked": 0, "untested": 0})
    if status in _APPLICABLE_STATUSES:
        cell["applicable"] += 1
        cell[status] = cell.get(status, 0) + 1


def rollup(coverage_ledger: list, catalog: list | None = None) -> dict:
    """Framework-mapped coverage from the ledger. Returns {objectives, owasp, asvs, posture}."""
    ledger = [c for c in (coverage_ledger or []) if isinstance(c, dict)]
    by_owasp: dict = {}
    by_asvs: dict = {}
    totals = {"applicable": 0, "tested": 0, "partial": 0, "blocked": 0, "untested": 0, "not_applicable": 0}
    for c in ledger:
        status = c.get("status", "untested")
        totals[status] = totals.get(status, 0) + 1
        if status in _APPLICABLE_STATUSES:
            totals["applicable"] += 1
        refs = c.get("refs") or {}
        _bump(by_owasp, str(refs.get("owasp") or "").split(":")[0].strip(), status)
        _bump(by_asvs, _asvs_chapter(refs.get("asvs")), status)
    applicable = totals["applicable"] or 1
    tested_frac = round(totals["tested"] / applicable, 3)
    posture = ("strong" if tested_frac >= 0.8 else "partial" if tested_frac >= 0.4 else "shallow")
    return {
        "objectives": totals,
        "owasp": by_owasp,
        "asvs": by_asvs,
        "tested_fraction_of_applicable": tested_frac,
        "posture": posture,
        "summary": (f"tested {totals['tested']}/{totals['applicable']} applicable objectives "
                    f"({totals['blocked']} blocked for missing identities, "
                    f"{totals['not_applicable']} not applicable); coverage posture: {posture}."),
    }
