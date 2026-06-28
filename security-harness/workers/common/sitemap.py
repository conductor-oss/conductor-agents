"""Sitemap enumeration for the §11 docs JS-render tier (P1-5).

A JS-rendered documentation site yields nothing to a plain fetch, so the docs adapter
enumerates the site's sitemap, filters to the security-relevant pages, and renders each
with the L4 Playwright browser. This module is the pure URL enumeration + relevance filter;
the render itself is browser-worker wiring that consumes ``security_relevant``.
"""

from __future__ import annotations

import re

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)

# The doc topics that carry security-relevant invariants worth ingesting (§4 docs input).
SECURITY_DOC_KEYWORDS = ["access", "rbac", "role", "permission", "auth", "secret", "tenant",
                         "token", "admin", "integration", "security", "webhook", "scope", "api",
                         "workflow", "task", "execution", "worker", "event", "schedule",
                         "inline", "http-task", "http_task", "definition", "guide", "tutorial"]


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


def security_relevant(xml: str, limit: int = 60) -> list:
    """The bounded set of doc pages worth rendering — the ones likely to state security
    invariants (RBAC, secrets, tenancy, auth, integrations…). Avoids rendering all 256 pages."""
    return urls(xml, contains=SECURITY_DOC_KEYWORDS, limit=limit)
