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

import base64
import json

# Claim keys that make good VICTIM MARKERS for cross-identity / cross-tenant oracles — a distinctive
# datum that belongs to THIS identity and would appear in another identity's read only on a real BOLA.
_MARKER_KEYS = ("id", "userId", "user_id", "uid", "sub", "email", "username", "name",
                "tenantId", "tenant_id", "orgId", "org_id", "accountId", "account_id")


def _b64url_decode(seg: str) -> bytes:
    seg = seg.strip().replace("-", "+").replace("_", "/")
    seg += "=" * (-len(seg) % 4)  # restore padding
    return base64.b64decode(seg)


def jwt_claims(auth_value) -> dict:
    """Best-effort decode of a JWT's payload claims (NO signature check — we only want the claims as
    identity markers). Accepts a raw JWT or a 'Bearer <jwt>'/'<scheme> <jwt>' header value. Returns {}
    on anything that isn't a JWT. Pure, never raises."""
    try:
        tok = str(auth_value or "").strip()
        if " " in tok:               # strip a scheme prefix like "Bearer "
            tok = tok.split()[-1]
        parts = tok.split(".")
        if len(parts) < 2:
            return {}
        claims = json.loads(_b64url_decode(parts[1]).decode("utf-8", "replace"))
        return claims if isinstance(claims, dict) else {}
    except Exception:                # noqa: BLE001 - any malformed token -> no markers
        return {}


def identity_markers(identities: dict | None) -> dict:
    """For each non-anon identity, pull its distinctive self-identifiers from its JWT claims so a
    cross-identity/cross-tenant agent gets a ready VICTIM MARKER instead of burning its budget
    discovering 'which resource does identity B own'. Returns {label: {marker_key: value, ...}}.

    Markers are harvested from top-level claims AND one level of nesting (Juice Shop puts the user in
    ``data``; many APIs nest under ``user``/``identity``/``principal``). Empty when no JWT is present."""
    out: dict[str, dict] = {}
    for label, spec in (identities or {}).items():
        if label == "anon" or not isinstance(spec, dict):
            continue
        claims = jwt_claims(spec.get("value"))
        if not claims:
            continue
        found: dict[str, str] = {}
        def _harvest(d: dict):
            for k, v in d.items():
                if k in _MARKER_KEYS and isinstance(v, (str, int)) and str(v).strip():
                    found.setdefault(k, str(v))
        _harvest(claims)
        for nest in ("data", "user", "identity", "principal", "account"):
            if isinstance(claims.get(nest), dict):
                _harvest(claims[nest])
        if spec.get("tenant"):
            found.setdefault("tenant", str(spec["tenant"]))
        if found:
            out[label] = found
    return out


def victim_marker_directive(identities: dict | None, exclude: str = "") -> str:
    """A one-line directive listing OTHER identities' markers for use as the oracle's victim_marker.
    `exclude` is the attacking identity's label (don't offer its own markers as a victim marker)."""
    markers = identity_markers(identities)
    others = {lbl: m for lbl, m in markers.items() if lbl != exclude}
    if not others:
        return ""
    parts = []
    for lbl, m in others.items():
        kv = ", ".join(f"{k}={v}" for k, v in list(m.items())[:4])
        parts.append(f"{lbl} ({kv})")
    return ("VICTIM MARKERS (use one of these distinctive values — a datum that belongs ONLY to the "
            "victim identity — as the oracle's victim_marker/injected_marker; prove it appears in the "
            "attacker's read of the victim's resource and is ABSENT from the attacker's own control): "
            + " | ".join(parts) + ".")


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
