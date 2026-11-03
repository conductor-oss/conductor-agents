"""Doc-surface extraction: turn ingested documentation into GROUNDED hypothesis seeds.

The run-05b95dd9 audit found the swarm *reads* docs (scouts call search_docs) but barely
*adapts*: only 1 of 36 hypotheses was doc-grounded, because ``docs_digest`` reached the
hypothesize LLM as unstructured prose with no instruction to convert documented endpoints and
invariants into attacks. This module mines the ingested chunks for the three things that make a
hypothesis: a reachable endpoint, the documented security rule guarding it (whose weakness or
silence is the bug to prove), and the feature families present. It emits deterministic
``seeds`` — concrete probes the hypothesize step must instantiate — so documentation measurably
drives hunting.

Pure functions over the chunk list; no I/O, no LLM.
"""

from __future__ import annotations

import re

# HTTP endpoints: "POST /api/secrets-v2/{key}" or bare "/api/integrations/provider/{name}".
_METHOD_PATH = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9_\-/{}.:]+)")
_BARE_PATH = re.compile(r"(?<![\w/])(/api/[A-Za-z0-9_\-/{}.:]+)")

# Documented security rules — the invariant a finding either violates or exposes as absent.
_INVARIANT_PATTERNS = {
    "authz": re.compile(
        r"(requires?\s+(?:admin|administrator|an?\s+\w+\s+role|[A-Z_]+\s+permission)"
        r"|only\s+admin|admin[- ]only|isAuthenticated|hasPermission|checkIfAuthorized"
        r"|does\s+not\s+(?:check|verify|enforce)|without\s+(?:checking|verifying))", re.I),
    "tenancy": re.compile(
        r"(per[- ]tenant|cross[- ]tenant|tenant\s+isolation|scoped\s+to\s+(?:the\s+)?(?:tenant|org|owner)"
        r"|other\s+tenants?|ownership|owned\s+by|doesn'?t\s+say|not\s+described|no\s+(?:documented\s+)?tenant"
        r"|inherit\w*\s+(?:\w+\s+)?access|existing\s+(?:permission\s+)?grant|through\s+the\s+existing)", re.I),
    "masking": re.compile(
        r"(mask(?:ed|ing)?|plaintext|redact(?:ed)?|secret\s+value|in\s+clear|exposed\s+in\s+(?:logs|output))", re.I),
    "ssrf": re.compile(
        r"(endpoint\b|\burl\b|\buri\b|callback|server\s+(?:fetches|makes|calls|connects)"
        r"|outbound\s+(?:request|call)|connectivity\s+test|discover(?:y)?\s+url|base[- ]?url)", re.I),
}

# High-value feature families and the attack each should always be probed for when the docs show
# the feature exists. Gated on the family actually appearing in ingested docs → grounded, not
# hardcoded blind spots. These were the exact zero-hypothesis misses in the audit.
_FAMILY = {
    "mcp": ("MCP gateway/integration",
            "register an MCP server/tool with an attacker URL or poisoned tool schema — prove SSRF "
            "to internal/metadata, tool-definition poisoning, or command/arg injection reaching exec"),
    "a2a": ("A2A (agent-to-agent) integration",
            "register/call a remote A2A agent with an attacker endpoint — prove SSRF and untrusted "
            "agent-response injection into the orchestrator"),
    "remote service": ("Remote Services registry",
                       "register a remote service/discovery URL the server probes — prove SSRF to "
                       "internal/metadata and cross-tenant service-definition overwrite"),
    "api gateway": ("API Gateway route config",
                    "create a gateway route/auth-config pointing at internal hosts or bypassing auth — "
                    "prove SSRF, route hijack, and GET/DELETE-by-id authz gaps"),
    "audit": ("Audit log API (/api/audit/*)",
              "if audit read is admin-only with no tenant scoping, prove cross-tenant audit read and "
              "use audit search as a secret-/resource-NAME enumeration oracle"),
    "tag": ("Tag-based access control",
            "assign a tag that already carries a broad grant to a resource you control (or a victim's) "
            "and prove tag-permission confusion → access you were never granted directly"),
    "status event": ("Workflow status events",
                     "point a workflow-status event/webhook sink at an internal host — prove SSRF and "
                     "cross-tenant status/info leak"),
    "webhook": ("Incoming webhooks / templates",
                "prove predictable/guessable webhook id (time-UUID) → cross-tenant trigger, and template "
                "resolution of ${secrets.*}/${workflow.*} → secret exfil"),
}


def _chunk_texts(chunks):
    out = []
    for c in chunks or []:
        if isinstance(c, dict):
            t = c.get("text") or ""
            src = (c.get("metadata") or {}).get("source") or c.get("source") or ""
            if t.strip():
                out.append((t, src))
    return out


def extract_endpoints(chunks, limit: int = 60) -> list:
    """Documented API endpoints, method where stated. Deduped by (method, path)."""
    seen, out = set(), []
    for text, src in _chunk_texts(chunks):
        pairs = [(m.upper(), p) for m, p in _METHOD_PATH.findall(text)]
        pathset = {p for _, p in pairs}
        pairs += [("", p) for p in _BARE_PATH.findall(text) if p not in pathset]
        for method, path in pairs:
            path = path.rstrip(".,);:")
            key = (method, path)
            if key in seen or len(path) < 5:
                continue
            seen.add(key)
            out.append({"method": method, "path": path, "source": src})
            if len(out) >= limit:
                return out
    return out


def extract_invariants(chunks, limit: int = 40) -> list:
    """Documented security rules (authz/tenancy/masking/ssrf) — each the invariant a finding
    violates or whose absence it exposes. One entry per matched sentence."""
    out = []
    for text, src in _chunk_texts(chunks):
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            s = sentence.strip()
            if not (12 <= len(s) <= 320):
                continue
            kinds = [k for k, rx in _INVARIANT_PATTERNS.items() if rx.search(s)]
            # ssrf alone is too noisy (every "url" matches); require it to co-occur with config intent
            if kinds == ["ssrf"] and not re.search(r"config|integration|register|provider|connect|endpoint", s, re.I):
                continue
            if kinds:
                out.append({"text": s[:320], "kinds": kinds, "source": src})
                if len(out) >= limit:
                    return out
    return out


def feature_families(chunks) -> list:
    """High-value feature families present in the ingested docs."""
    blob = " ".join(t for t, _ in _chunk_texts(chunks)).lower()
    return [fam for fam in _FAMILY if fam in blob]


def build_surface(chunks) -> dict:
    """Structured attack surface + grounded hypothesis seeds derived from the docs. ``seeds`` are
    concrete probes the hypothesize step must instantiate into hypotheses."""
    endpoints = extract_endpoints(chunks)
    invariants = extract_invariants(chunks)
    families = feature_families(chunks)
    seeds = []

    # 1) Family seeds — one per documented high-value feature family (the audit's zero-hyp misses).
    for fam in families:
        label, attack = _FAMILY[fam]
        seeds.append(f"DOC-SURFACE [{label}]: {attack}.")

    # 2) Endpoint+invariant seeds — pair an endpoint with the weakest documented rule near it.
    weak = [iv for iv in invariants if ("authz" in iv["kinds"] or "tenancy" in iv["kinds"])]
    for ep in endpoints[:24]:
        path_tokens = set(re.findall(r"[a-z]+", ep["path"].lower()))
        match = next((iv for iv in weak
                      if path_tokens & set(re.findall(r"[a-z]+", iv["text"].lower()))), None)
        disp = (ep["method"] + " " + ep["path"]).strip()
        if match:
            seeds.append(f"DOC-SURFACE: {disp} — documented rule: \"{match['text']}\". "
                         f"Probe cross-tenant/BOLA/privilege-boundary violation of this rule.")
        else:
            seeds.append(f"DOC-SURFACE: {disp} — no authorization rule documented here; "
                         f"probe missing object-level authz (BOLA) and privilege boundary.")

    # 3) INVARIANT-TEST seeds — the heart of system-level (not URL-level) pentesting: the docs state
    # rules the system claims to enforce; each one becomes "prove the system does NOT enforce it".
    # The LLM docs_digest returns these only sometimes (run 05b95dd9: 0 invariants), so extracting
    # them deterministically here guarantees the swarm tests the SYSTEM'S SECURITY MODEL, not just
    # endpoints. One seed per distinct invariant kind, de-duplicated by text.
    _KIND_PROBE = {
        "authz": "prove this authorization rule is NOT enforced (a lower-privileged caller performs it)",
        "tenancy": "prove this tenancy/ownership boundary is crossable (another tenant/user reaches it)",
        "masking": "prove this masking/secrecy rule is violated (the secret reaches output/logs/prompt)",
    }
    seen_inv = set()
    for iv in invariants:
        txt = iv["text"].strip()
        if txt in seen_inv:
            continue
        seen_inv.add(txt)
        for kind in ("authz", "tenancy", "masking"):          # ssrf handled by endpoint/family seeds
            if kind in iv["kinds"]:
                seeds.append(f"DOC-SURFACE [invariant/{kind}]: docs state \"{txt}\" — {_KIND_PROBE[kind]}.")
                break

    # dedupe, preserve order, bound
    seen, uniq = set(), []
    for s in seeds:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    inv_seeds = sum(1 for s in uniq if "[invariant/" in s)
    return {
        "endpoints": endpoints,
        "invariants": invariants,
        "families": families,
        "seeds": uniq[:60],
        "summary": f"{len(endpoints)} documented endpoints, {len(invariants)} invariants "
                   f"({inv_seeds} as invariant-tests), {len(families)} high-value feature families "
                   f"→ {len(uniq[:60])} hypothesis seeds",
    }


# ── docstore I/O (kept separate from the pure core above) ─────────────────────────────────────
def _docstore_path(run_id: str):
    import os
    import re as _re
    try:
        from common import memory as _memory
        base = _memory.state_dir()
    except Exception:
        base = os.path.join(os.path.dirname(__file__), "..", "state")
    safe = _re.sub(r"[^A-Za-z0-9._-]", "_", str(run_id))[:120]
    return os.path.join(base, "docs", f"{safe}.json")


def load_chunks(run_id: str) -> list:
    """Load the per-run ingested doc chunks written by rag.ingest_docs, or [] if absent."""
    import json
    import os
    p = _docstore_path(run_id)
    if not os.path.exists(p):
        return []
    try:
        with open(p) as fh:
            return json.load(fh) or []
    except Exception:
        return []


def surface_for_run(run_id: str) -> dict:
    """Convenience: build the doc surface for a run from its persisted docstore. Empty surface
    (no seeds) when the run has no ingested docs."""
    chunks = load_chunks(run_id)
    if not chunks:
        return {"endpoints": [], "invariants": [], "families": [], "seeds": [], "summary": "no docs"}
    return build_surface(chunks)


# Family → objective id for board coverage/credit accounting (deterministic, grounded in docs).
_FAMILY_OBJECTIVE = {
    "mcp": "ssrf-mcp-gateway",
    "a2a": "ssrf-a2a-integration",
    "remote service": "ssrf-remote-service",
    "api gateway": "authz-api-gateway-config",
    "audit": "authz-audit-crosstenant",
    "tag": "privesc-tag-confusion",
    "status event": "ssrf-status-event",
    "webhook": "idor-webhook-crosstenant",
}


def seed_hypotheses(run_id: str) -> list:
    """Doc-grounded hypotheses ready to post to the board (one per seed). Mirrors the CVE-lead
    seeding path: deterministic, so documented high-value surface is ALWAYS hunted in round 1,
    independent of whether the hypothesize LLM chooses to instantiate it."""
    surface = surface_for_run(run_id)
    seeds = surface.get("seeds") or []
    families = surface.get("families") or []
    _INVARIANT_OBJECTIVE = {
        "invariant/authz": "authz-invariant-violation",
        "invariant/tenancy": "crosstenant-invariant-violation",
        "invariant/masking": "secret-masking-violation",
    }
    hyps = []
    for s in seeds:
        oid = "authz-doc-endpoint"
        m = __import__("re").match(r"DOC-SURFACE \[([^\]]+)\]", s)
        if m:
            label = m.group(1).lower()
            if label in _INVARIANT_OBJECTIVE:                       # documented security-rule tests
                oid = _INVARIANT_OBJECTIVE[label]
            else:
                oid = next((o for fam, o in _FAMILY_OBJECTIVE.items()
                            if fam in label or label.split()[0] in fam), "doc-surface-probe")
        elif "no authorization rule documented" in s:
            oid = "bola-doc-endpoint"
        hyps.append({
            "title": s[:200],
            "objective_id": oid,
            "rationale": "Grounded in the target's own documentation (doc-surface extraction): "
                         "documented endpoint/invariant/feature the team has not yet hunted.",
            "source": "doc_surface",
        })
    return hyps
