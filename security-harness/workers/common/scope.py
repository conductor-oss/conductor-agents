"""Scope enforcement — the safety core of the scanner.

Every worker that makes a request to the target MUST funnel the URL through
``enforce()`` (or check ``in_scope()``) so an automated crawl/scan can never
wander off the operator-authorized target. This is what keeps the tool on the
right side of "authorized testing only".

A scope is a plain dict so it round-trips cleanly through Conductor task I/O:

    {
      "in_scope_hosts": ["juice-shop.example"],   # hosts explicitly allowed
      "allow_subdomains": false,                    # also allow *.host if true
      "exclude_patterns": ["/logout", "signout"]    # substrings to never touch
    }
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlparse


class OutOfScopeError(Exception):
    """Raised when a URL falls outside the authorized scope."""


def _host_of(url: str) -> str:
    netloc = urlparse(url if "://" in url else f"//{url}", scheme="http").netloc
    return (netloc.split("@")[-1].split(":")[0] or "").lower()


def derive_scope(target_url: str, allow_subdomains: bool = False,
                 exclude_patterns: Iterable[str] | None = None) -> dict:
    """Build a default scope that permits only the target's own host."""
    host = _host_of(target_url)
    if not host:
        raise ValueError(f"cannot derive scope: no host in target {target_url!r}")
    return {
        "in_scope_hosts": [host],
        "allow_subdomains": bool(allow_subdomains),
        "exclude_patterns": list(exclude_patterns or []),
    }


def normalize_scope(scope: dict | None, target_url: str) -> dict:
    """Coerce a possibly-partial scope dict into a complete one."""
    if not scope or not scope.get("in_scope_hosts"):
        return derive_scope(target_url, allow_subdomains=bool((scope or {}).get("allow_subdomains")),
                            exclude_patterns=(scope or {}).get("exclude_patterns"))
    return {
        "in_scope_hosts": [h.lower() for h in scope["in_scope_hosts"]],
        "allow_subdomains": bool(scope.get("allow_subdomains", False)),
        "exclude_patterns": list(scope.get("exclude_patterns", [])),
    }


def in_scope(url: str, scope: dict) -> bool:
    """True iff ``url``'s host is allowed and it matches no exclude pattern."""
    host = _host_of(url)
    if not host:
        return False
    for pat in scope.get("exclude_patterns", []):
        if pat and pat in url:
            return False
    allowed = scope.get("in_scope_hosts", [])
    if host in allowed:
        return True
    if scope.get("allow_subdomains"):
        return any(host == a or host.endswith("." + a) for a in allowed)
    return False


def enforce(url: str, scope: dict) -> str:
    """Return ``url`` if in scope, else raise :class:`OutOfScopeError`."""
    if not in_scope(url, scope):
        raise OutOfScopeError(
            f"{url} is outside the authorized scope {scope.get('in_scope_hosts')}"
        )
    return url
