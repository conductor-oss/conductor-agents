"""Frozen-contract discipline for the convergence spike (roadmap H4, Phase 1).

Real teams build in parallel only behind interfaces they agree on first and do not change under each
other. For the spike, a concrete contract artifact (an interface/signature file plus the shared tests
that pin it) is committed to the shared branch and treated as **fixed**. A candidate diff that edits
the frozen contract is not an ordinary change: it is a **controlled event** — it must become its own
board item that re-plans dependents — never an ambient edit silently merged.

This module is pure logic (path classification against the declared contract set); the workflow calls
it to classify each candidate's changed paths before integration. It does not itself edit or merge —
it decides whether a change is contract-respecting or a controlled event.
"""

from __future__ import annotations

import posixpath


def _norm(path: str) -> str:
    """Normalize a repo-relative path for comparison (forward slashes, no leading ``./`` or ``/``)."""
    p = str(path or "").strip().replace("\\", "/")
    p = posixpath.normpath(p)
    return "" if p in (".", "") else p.lstrip("/")


def _is_under(path: str, base: str) -> bool:
    """True if ``path`` equals ``base`` or is contained within it (treating ``base`` as a file or dir)."""
    path, base = _norm(path), _norm(base)
    if not base:
        return False
    return path == base or path.startswith(base + "/")


def contract_paths_touched(changed_paths, contract_paths) -> list[str]:
    """Return the sorted subset of ``changed_paths`` that fall on (or inside) any declared contract
    path. A contract entry may name a file or a directory; both match."""
    changed = [c for c in (changed_paths or []) if str(c or "").strip()]
    contracts = [c for c in (contract_paths or []) if str(c or "").strip()]
    hit = {_norm(c) for c in changed if any(_is_under(c, b) for b in contracts)}
    return sorted(p for p in hit if p)


def classify_change(changed_paths, contract_paths) -> dict:
    """Classify a candidate diff relative to the frozen contract.

    Returns ``{touches_contract, contract_paths_touched, event}`` where ``event`` is ``"controlled"``
    when the diff edits the frozen contract (requires its own board item + re-plan of dependents) or
    ``"ordinary"`` when it builds against the contract without changing it (eligible for integration)."""
    touched = contract_paths_touched(changed_paths, contract_paths)
    touches = bool(touched)
    return {
        "touches_contract": touches,
        "contract_paths_touched": touched,
        "event": "controlled" if touches else "ordinary",
    }


def is_integration_eligible(changed_paths, contract_paths) -> bool:
    """A candidate is eligible for ordinary continuous integration only if it does not edit the frozen
    contract. A contract-touching change must route through the controlled-event path instead."""
    return not classify_change(changed_paths, contract_paths)["touches_contract"]
