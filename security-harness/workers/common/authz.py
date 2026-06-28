"""Machine-enforceable authorization manifest + capability levels.

The harness must not authorize itself or enlarge its own scope (spec section 2).
A campaign therefore carries an *authorization manifest*: a plain dict (so it
round-trips through Conductor task I/O) describing exactly what is permitted —
who approved it, which hosts, the testing window, the maximum capability level,
rate/volume budgets, and forbidden operations.

This module is pure logic (no network, only the wall clock) so it is unit-testable
in isolation, mirroring ``scope.py``. Two jobs:

  * ``validate``  -- decide whether a campaign may run at all (fails closed).
  * ``action_capability`` / ``forbids`` / ``technique_allowed`` -- per-action gates
    the executor workers consult before they touch the target.

Capability levels (spec section 15.1):
  0  passive reading / observation
  1  reversible, low-volume active tests (GET/HEAD/OPTIONS)
  2  state-changing tests using synthetic data (POST/PUT/PATCH/DELETE, code_exec)
  3  potentially sensitive / operationally risky proof (just-in-time approval)
  4  destructive / availability-impacting / real-data extraction (prohibited by default)

The harness cannot raise its own level: ``capability_max`` comes from the manifest
and every worker refuses an action whose required level exceeds it.
"""

from __future__ import annotations

import fnmatch
from datetime import datetime, timezone
from urllib.parse import urlparse

# Capability levels by HTTP method / action kind.
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Required-field minimum for a manifest to be considered explicit authorization.
_REQUIRED = ("approvers", "in_scope_hosts", "window")


def _host_of(url: str) -> str:
    netloc = urlparse(url if "://" in url else f"//{url}", scheme="http").netloc
    return (netloc.split("@")[-1].split(":")[0] or "").lower()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Accept a trailing Z (Python < 3.11 doesn't on fromisoformat for "Z").
        v = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def validate(manifest: dict | None, target: str, now: datetime | None = None) -> dict:
    """Decide whether a campaign is authorized. Fails closed.

    Returns ``{ok, reason, capability_max}``. ``ok`` is True only when the manifest
    is structurally complete, the wall clock is inside the testing window, and the
    target's host is explicitly in scope. Any ambiguity -> ``ok=False`` (spec 15).
    """
    if not isinstance(manifest, dict) or not manifest:
        return {"ok": False, "reason": "no authorization manifest supplied", "capability_max": 0}

    missing = [k for k in _REQUIRED if not manifest.get(k)]
    if missing:
        return {"ok": False, "reason": f"manifest missing required field(s): {', '.join(missing)}",
                "capability_max": 0}

    window = manifest.get("window") or {}
    now = now or _now()
    start = _parse_iso(window.get("start"))
    expiry = _parse_iso(window.get("expiry"))
    if expiry is None:
        return {"ok": False, "reason": "manifest window has no parseable expiry", "capability_max": 0}
    if start and now < start:
        return {"ok": False, "reason": f"testing window has not started (starts {window.get('start')})",
                "capability_max": 0}
    if now > expiry:
        return {"ok": False, "reason": f"authorization expired at {window.get('expiry')}",
                "capability_max": 0}

    hosts = [str(h).lower() for h in (manifest.get("in_scope_hosts") or [])]
    thost = _host_of(target)
    if not thost:
        return {"ok": False, "reason": f"cannot determine target host from {target!r}", "capability_max": 0}
    allow_sub = bool(manifest.get("allow_subdomains"))
    in_scope = thost in hosts or (allow_sub and any(thost == h or thost.endswith("." + h) for h in hosts))
    if not in_scope:
        return {"ok": False, "reason": f"target host {thost!r} is not in the manifest scope {hosts}",
                "capability_max": 0}

    cap = manifest.get("capability_max", 1)
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = 1
    cap = max(0, min(cap, 4))
    return {"ok": True, "reason": "authorized", "capability_max": cap}


def action_capability(method: str, is_code_exec: bool = False, is_sensitive: bool = False) -> int:
    """Required capability level for one action."""
    if is_code_exec:
        return 3 if is_sensitive else 2
    m = (method or "GET").upper()
    if m in _READ_METHODS:
        return 1
    if m in _WRITE_METHODS:
        return 3 if is_sensitive else 2
    return 2  # unknown verb -> treat as state-changing


def forbids(method: str, url: str, manifest: dict | None) -> bool:
    """True iff ``method url`` matches a manifest ``forbidden_operations`` glob or
    touches a ``protected_records`` token. Patterns are matched against
    ``"<METHOD> <path>"`` and the full URL, case-insensitively."""
    if not isinstance(manifest, dict):
        return False
    m = (method or "GET").upper()
    path = urlparse(url).path or url
    candidates = (f"{m} {path}".lower(), f"{m} {url}".lower(), url.lower())
    for pat in manifest.get("forbidden_operations") or []:
        p = str(pat).lower()
        if any(fnmatch.fnmatch(c, p) for c in candidates):
            return True
    for rec in manifest.get("protected_records") or []:
        if str(rec).lower() in url.lower():
            return True
    return False


def resilience_allowed(manifest: dict | None) -> bool:
    """Resilience/availability testing is OFF by default (it applies bounded load): it
    runs only when the manifest explicitly lists 'resilience' in allowed_classes."""
    if not isinstance(manifest, dict):
        return False
    return "resilience" in {str(c).lower() for c in (manifest.get("allowed_classes") or [])}


def technique_allowed(category: str, manifest: dict | None) -> bool:
    """True iff a hypothesis/technique category is permitted. When the manifest
    lists no ``allowed_techniques`` the default is permissive (all allowed)."""
    if not isinstance(manifest, dict):
        return True
    allowed = manifest.get("allowed_techniques")
    if not allowed:
        return True
    return str(category).lower() in {str(a).lower() for a in allowed}
