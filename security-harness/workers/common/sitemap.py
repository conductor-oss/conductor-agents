"""Sitemap enumeration for the §11 docs JS-render tier (P1-5).

A JS-rendered documentation site yields nothing to a plain fetch, so the docs adapter
enumerates the site's sitemap, filters to the security-relevant pages, and renders each
with the L4 Playwright browser. This module is the pure URL enumeration + relevance filter;
the render itself is browser-worker wiring that consumes ``security_relevant``.

Selection is RANKED, not first-N-in-file-order. A docs sitemap lists category/overview pages
alongside the deep reference leaves that actually state the attack surface (concrete endpoints,
params, authz rules). Taking the first N in sitemap order buries the leaves under nav pages.
``security_relevant`` scores every candidate and returns the highest-value pages first so the
bounded render budget is spent on surface-bearing leaves (``/reference-docs/api/*``), not
``/category/*`` index pages.
"""

from __future__ import annotations

import re

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)

# The doc topics that carry security-relevant invariants worth ingesting (§4 docs input).
# Keep this broad: a missing keyword means a whole FEATURE FAMILY never gets ingested and so
# never gets hypothesized. The audit of run 05b95dd9 found MCP Gateway, A2A, remote services,
# audit API, and tag-permission pages were absent precisely because their keywords were missing.
SECURITY_DOC_KEYWORDS = [
    # access control / identity
    "access", "rbac", "role", "permission", "auth", "secret", "tenant", "token", "admin",
    "scope", "user", "group", "application", "service-account", "sign", "verif", "audit", "tag",
    # integrations / outbound (SSRF-rich)
    "integration", "provider", "remote", "gateway", "mcp", "a2a", "agent", "webhook", "event",
    "connect", "environment", "http-task", "http_task", "service",
    # orchestration surface
    "security", "api", "workflow", "task", "execution", "worker", "schedule", "signal", "human",
    "inline", "definition", "prompt", "share", "idempoten", "proto", "registry", "metadata",
    # learning material (how-to pages teach correct usage → reveal invariants to violate)
    "guide", "tutorial", "reference", "how-to", "howto",
]

# Auto-generated endpoint reference stubs — one per endpoint, thin, ~200 of them. Valuable for
# endpoint/param NAMES but they must not monopolize the render budget (they'd crowd out the prose
# pages that state the authz/tenancy RULES). Sampled with a per-group diversity cap instead.
_REF_PATH = ("/reference-docs/api/", "/reference-docs/", "/api-reference/")
# Overview / index / nav pages — real but low-density; rank below leaves.
_LOW_VALUE_PATH = ("/category/", "/docs/category/")
# Concept/how-to prose sections — these state security invariants and explain feature usage, so
# they are the richest source of hypotheses. Boosted above reference stubs.
_CONCEPT_PATH = ("/access-control-and-security/", "/developer-guides/", "/devguide/",
                 "/conceptual-guides/", "/how-tos/", "/tutorials/", "/templates/", "/eventing",
                 "/remote-services", "/webhook-templates", "/workers")
# Max reference-stub pages kept per API group (gateway, secrets, …) so the sample spans the API
# surface instead of 23 near-duplicate endpoints from one group.
_REF_PER_GROUP = 3
# Fraction of the render budget reserved for the diverse reference-stub sample (endpoint/param
# names). The rest goes to invariant-bearing concept pages.
_REF_BUDGET_FRAC = 3   # 1/3
# Critical single pages that must be rendered whenever present — each carries an authz/tenancy
# invariant or a whole feature family's surface that the swarm must hunt. Guaranteed a slot so a
# crowd of equally-scoring guides can never push them out.
_GUARANTEED = (
    "/access-control-and-security/", "audit-logs", "/tags", "users-and-groups", "applications",
    "mcp-gateway", "mcp-api-gateway", "a2a-integration", "remote-services", "api-gateway",
    "secrets-in-conductor", "secrets", "webhook-templates", "webhook-integration",
    "workflow-status-events", "idempotency",
)
# Pure noise — never render.
_NOISE = ("/blog/", "/marketing", "/pricing", "/customers", "/careers", "/privacy-policy",
          "/terms", "/cookie", "/newsletter", "/events/", "/webinar")


def urls(xml: str, *, contains=None, limit: int = 200) -> list:
    """Extract <loc> URLs from a sitemap, optionally keeping only those whose URL contains any
    of ``contains`` (case-insensitive). Stable order, deduped, capped."""
    found = _LOC.findall(xml or "")
    if contains:
        kws = [c.lower() for c in contains]
        found = [u for u in found if any(k in u.lower() for k in kws)]
    seen, out = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= limit:
            break
    return out


def _is_ref(u: str) -> bool:
    return any(p in u for p in _REF_PATH)


def _ref_group(u: str) -> str:
    """The API group of a reference stub (e.g. 'gateway', 'secrets') for diversity capping."""
    for p in _REF_PATH:
        if p in u:
            tail = u.split(p, 1)[1].strip("/").split("/")
            return tail[0] if tail else "_"
    return "_"


def _score(url: str) -> int:
    """Relevance score for a candidate doc URL. Higher = render sooner.

    Rewards keyword breadth (distinct security topics, capped so no single multi-keyword URL
    dominates) and concept/how-to prose pages (which state the authz/tenancy RULES and so seed the
    most hypotheses); reference stubs score positive but modest (sampled separately); category
    indexes are penalized. Zero-keyword pages score < 0 and are dropped."""
    u = url.lower()
    if any(n in u for n in _NOISE):
        return -10_000
    kws = [k for k in SECURITY_DOC_KEYWORDS if k in u]
    if not kws:
        return -1
    score = 8 * min(len(set(kws)), 4)               # breadth, capped at 4 topics (+32 max)
    if any(p in u for p in _CONCEPT_PATH):          # prose/how-to: states invariants → best seeds
        score += 45
    elif _is_ref(u):                                # concrete endpoint/param stub: useful, modest
        score += 20
    if any(p in u for p in _LOW_VALUE_PATH):        # overview/nav index — keep but deprioritize
        score -= 25
    depth = url.count("/") - 2                       # leaf depth: more specific, capped
    score += min(max(depth, 0), 8) * 3
    return score


def rank(xml: str, *, limit: int = 60) -> list:
    """Blended, diversity-capped selection of the highest-value doc pages.

    Phases: (1) guaranteed critical invariant/feature pages; (2) a per-group-capped sample of
    reference stubs filling a reserved ~1/3 of the budget (endpoint/param breadth); (3) top concept/
    prose pages (which state the authz/tenancy rules); (4) leftover refs, then (5) other/category
    pages. Deduped, capped at ``limit`` — so the budget always covers the rules AND a broad slice of
    the endpoint surface, never 200 near-duplicate stubs nor a crowd of guides burying key pages."""
    seen, scored = set(), {}
    concept, refs, other, guaranteed = [], [], [], []
    for u in _LOC.findall(xml or ""):
        if u in seen:
            continue
        seen.add(u)
        s = _score(u)
        if s <= 0:
            continue
        scored[u] = s
        order = len(seen)
        # Guarantee only CONCEPT pages (not reference stubs) — otherwise a slug like "remote-services"
        # would pull all ~16 of that group's endpoint stubs past the per-group diversity cap.
        if not _is_ref(u) and any(g in u.lower() for g in _GUARANTEED):
            guaranteed.append((s, order, u))
        if _is_ref(u):
            refs.append((s, order, u))
        elif any(p in u for p in _CONCEPT_PATH):
            concept.append((s, order, u))
        else:
            other.append((s, order, u))
    for lst in (guaranteed, concept, refs, other):
        lst.sort(key=lambda t: (-t[0], t[1]))
    # diversity-cap the reference stubs: at most _REF_PER_GROUP per API group, best-scoring first
    per_group, ref_keep = {}, []
    for _, _, u in refs:
        g = _ref_group(u)
        if per_group.get(g, 0) >= _REF_PER_GROUP:
            continue
        per_group[g] = per_group.get(g, 0) + 1
        ref_keep.append(u)

    out, s2 = [], set()
    def _add(u):
        if u not in s2 and len(out) < limit:
            s2.add(u)
            out.append(u)

    for _, _, u in guaranteed:                       # (1) always-render critical pages
        _add(u)
    ref_slots = max(0, (limit - len(out)) // _REF_BUDGET_FRAC)
    for u in ref_keep[:ref_slots]:                   # (2) reserved diverse endpoint sample
        _add(u)
    for _, _, u in concept:                          # (3) invariant-bearing prose
        _add(u)
    for u in ref_keep[ref_slots:]:                   # (4) any remaining endpoint refs
        _add(u)
    for _, _, u in other:                            # (5) everything else, incl. category
        _add(u)
    return out


def security_relevant(xml: str, limit: int = 60) -> list:
    """The bounded set of doc pages worth rendering — the ones likely to state security invariants
    (RBAC, secrets, tenancy, auth, integrations, remote/MCP/A2A sinks…). Blended so the render
    budget covers the invariant-bearing prose pages AND a diverse sample of concrete endpoint
    references, instead of whatever appears first in the sitemap or 200 near-duplicate stubs."""
    return rank(xml, limit=limit)
