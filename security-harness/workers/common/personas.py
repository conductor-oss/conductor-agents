"""Attacker personas as first-class objects (spec section 8).

The harness should reason from attacker GOALS, not only vulnerability categories.
The spec lists representative personas (anonymous attacker, ordinary user, malicious
tenant, compromised privileged user, ...). This module maps the identities actually
supplied to a campaign onto those personas, each carrying initial knowledge, a
starting position, objectives, success conditions, and operational constraints, so
the hypothesis/exploit prompts can pursue an objective rather than just flip an id.

Pure logic, unit-testable; mirrors scope.py / authz.py.
"""

from __future__ import annotations

# Ordered persona templates. Each identity is matched to the best-fitting template
# by its label/role; `anon` is always the anonymous-attacker persona.
_TEMPLATES = [
    {
        "match": ["anon", "anonymous", "public", "guest"],
        "name": "anonymous internet attacker",
        "initial_knowledge": "only what is reachable unauthenticated",
        "objectives": ["reach data or actions that should require authentication",
                       "find unauth admin/debug endpoints", "enumerate other users' resources"],
        "success_conditions": ["any protected data or action obtained without credentials"],
    },
    {
        "match": ["admin", "owner", "root", "superuser"],
        "name": "compromised privileged user",
        "initial_knowledge": "full privileged access to one account/tenant",
        "objectives": ["reach OTHER tenants' data", "read stored secrets/integration tokens",
                       "perform privileged actions beyond the account's own scope",
                       "retain access after revocation"],
        "success_conditions": ["cross-tenant data/secret read", "action affecting another tenant"],
    },
]

# The default template for any authenticated, non-admin identity.
_ORDINARY = {
    "name": "ordinary authenticated user / malicious tenant",
    "initial_knowledge": "a normal account in one tenant",
    "objectives": ["read another user's or tenant's data (BOLA/IDOR)",
                   "escalate privileges (mass-assignment, role change)",
                   "break a documented business invariant (limit/quota/one-time action)",
                   "abuse a workflow's state machine (skip/replay/reorder steps)"],
    "success_conditions": ["cross-identity data read", "privileged action as a low-priv user",
                           "a persisted illegal state"],
}


def _template_for(label: str) -> dict:
    low = (label or "").lower()
    for t in _TEMPLATES:
        if any(tok in low for tok in t["match"]):
            return t
    return _ORDINARY


def build(identities: dict | None, app_model: dict | None = None,
          manifest: dict | None = None) -> list[dict]:
    """Map supplied identities onto attacker personas (spec 8).

    ``identities`` is the ``{label: {header, value}}`` map from resolve_identities.
    Only personas permitted by the manifest's ``allowed_personas`` (when set) are
    emitted. Cross-tenant objectives are only meaningful with >=2 privileged
    identities; that is annotated on each persona.
    """
    identities = identities or {}
    allowed = None
    if isinstance(manifest, dict) and manifest.get("allowed_personas"):
        allowed = {str(p).lower() for p in manifest["allowed_personas"]}

    priv_labels = [lbl for lbl, v in identities.items()
                   if lbl != "anon" and isinstance(v, dict) and v.get("value")]
    cross_tenant_possible = len(priv_labels) >= 2

    out = []
    for label in identities:
        if allowed is not None and label.lower() not in allowed:
            continue
        tmpl = _template_for(label)
        constraints = ["stay in scope", "synthetic data only", "minimum-impact proof"]
        objectives = list(tmpl["objectives"])
        if not cross_tenant_possible:
            objectives = [o for o in objectives if "tenant" not in o.lower()] or objectives
            constraints.append("cross-tenant objectives need a 2nd distinct-tenant identity (not supplied)")
        out.append({
            "label": label,
            "persona": tmpl["name"],
            "identity": label,
            "initial_knowledge": tmpl["initial_knowledge"],
            "start_position": "authenticated as this identity" if label != "anon" else "unauthenticated",
            "objectives": objectives,
            "success_conditions": tmpl["success_conditions"],
            "constraints": constraints,
        })
    return out
