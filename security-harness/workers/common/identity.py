"""Identity-adequacy gate (ROADMAP E1).

An honest authorization/tenancy verdict depends on the *matrix* of identities supplied.
Cross-tenant isolation cannot be proven with one credential; cross-user (BOLA) needs two
users; vertical privesc needs a non-privileged starting point. This module classifies the
supplied identities and reports which objective classes are actually PROVABLE — so the
coverage ledger can mark the rest **blocked (need more identities)** instead of silently
reporting a single-identity "clean" (the exact false-negative the Orkes runs produced).

Distinct *tenants* can't be inferred from a bearer token, so we treat each distinct
privileged identity as a potential distinct tenant unless the operator pins a `tenant`
field on the `--id` spec. We say so in the note (honesty over false precision).
"""

from __future__ import annotations


def classify(identities: dict | None) -> dict:
    """Return an adequacy report for the supplied {label: {header,value,tenant?}} map."""
    identities = identities if isinstance(identities, dict) else {}
    has_anon = "anon" in identities
    privileged = [(lbl, v) for lbl, v in identities.items()
                  if lbl != "anon" and isinstance(v, dict) and v.get("value")]
    n_priv = len(privileged)
    # Distinct tenants: explicit `tenant` field if the operator supplied it, else assume
    # each distinct privileged identity is a distinct tenant (optimistic, flagged in note).
    tenants = {v.get("tenant") for _, v in privileged if v.get("tenant")}
    explicit_tenants = len(tenants)
    distinct_tenants = explicit_tenants if explicit_tenants else n_priv

    adequacy = {
        "authenticated": n_priv >= 1,            # can test authenticated surface at all
        "cross_user": n_priv >= 2,               # BOLA across two principals
        "cross_tenant": distinct_tenants >= 2,   # tenant isolation
        "privesc": n_priv >= 1,                  # vertical escalation attemptable
        "n_privileged": n_priv,
        "distinct_tenants": distinct_tenants,
        "tenants_explicit": explicit_tenants > 0,
        "has_anon": has_anon,
    }
    adequacy["note"] = _note(adequacy)
    return adequacy


def _note(a: dict) -> str:
    if a["n_privileged"] == 0:
        return "no privileged identity supplied: only the unauthenticated surface is assessable."
    parts = [f"{a['n_privileged']} privileged identity(ies)"]
    if not a["cross_tenant"]:
        parts.append("cross-tenant/BOLA isolation NOT provable (need >=2 distinct-tenant credentials)")
    elif not a["tenants_explicit"]:
        parts.append("treating distinct identities as distinct tenants (pass tenant:<id> on --id to be explicit)")
    return "; ".join(parts) + "."


# Map an objective's `required_identities` note to the adequacy key it needs.
def required_key(required_identities: str | None) -> str | None:
    ri = (required_identities or "").lower()
    if "tenant" in ri:
        return "cross_tenant"
    if "user" in ri and ("2" in ri or "two" in ri or ">=2" in ri or "+" in ri):
        return "cross_user"
    if "low-priv" in ri or "privileged" in ri:
        return "privesc"
    return None


def blocked_by_adequacy(required_identities: str | None, adequacy: dict | None) -> bool:
    """True iff this objective needs an identity capability the campaign doesn't have."""
    if not adequacy:
        return False
    key = required_key(required_identities)
    if key is None:
        return False
    return not bool(adequacy.get(key))
