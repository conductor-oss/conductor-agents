"""Automatic halt conditions (spec section 15.2).

Testing must stop when an action crosses a line the operator did not authorize:
real sensitive data is unexpectedly accessed, an effect crosses the authorized
scope/tenant boundary, a forbidden operation is attempted, or a rate/volume budget
is exceeded.

A worker cannot itself terminate the workflow, so ``evaluate`` is a pure predicate:
the executor (http_request / code_exec) calls it after an action and, if it trips,
records a ``halt_requested`` flag in its result. The workflow accumulates that flag
and the safety governor (``workers/safety``) terminates the campaign at the next
pass boundary. Within-action *refusals* (capability / scope / forbidden) are enforced
immediately by the worker before the request is sent.
"""

from __future__ import annotations

from urllib.parse import urlparse

from common import authz

# How many distinct real secrets/PII hits in one response is "unexpected bulk access".
_SENSITIVE_TRIP = 5


def _host_of(url: str) -> str:
    netloc = urlparse(url if "://" in url else f"//{url}", scheme="http").netloc
    return (netloc.split("@")[-1].split(":")[0] or "").lower()


def evaluate(observation: dict, manifest: dict | None, scope: dict | None,
             counters: dict | None = None) -> dict:
    """Return ``{halt: bool, reason: str}`` for one observed action.

    ``observation`` carries: ``method``, ``url``, ``final_url`` (post-redirect),
    ``sensitive`` (the ``common.sensitive.scan`` result). ``counters`` carries the
    running ``requests`` and ``bytes`` totals for budget checks.
    """
    observation = observation or {}
    manifest = manifest if isinstance(manifest, dict) else {}
    counters = counters or {}

    method = observation.get("method") or "GET"
    url = observation.get("url") or ""
    final_url = observation.get("final_url") or url

    # 1) Forbidden operation attempted.
    if authz.forbids(method, url, manifest):
        return {"halt": True, "reason": f"forbidden operation attempted: {method} {url}"}

    # 2) Effect crossed the authorized scope boundary (e.g. an unexpected redirect to
    #    an out-of-scope/other-tenant host actually followed).
    if scope and final_url:
        from common import scope as scope_mod  # local import: avoid cycle at module load
        if not scope_mod.in_scope(final_url, scope):
            return {"halt": True,
                    "reason": f"effect crossed scope boundary: landed on {_host_of(final_url)!r}"}

    # 3) Unexpected bulk access to real sensitive data.
    sens = observation.get("sensitive") or {}
    if sens.get("found"):
        total = sum((sens.get("secrets") or {}).values()) + sum((sens.get("pii") or {}).values())
        # The target's own authorized in-scope hosts return the user's own data BY DESIGN (an
        # admin reading its org's users/clusters). Those are expected data sources — the bulk
        # halt is meant for UNEXPECTED exfil (the target SSRFing to metadata, a response leaking
        # another tenant's data on an out-of-scope host), which condition (2) + out-of-scope hosts
        # cover. So expected = explicit expected_data_hosts ∪ the authorized scope.
        expected = {str(h).lower() for h in (manifest.get("expected_data_hosts") or [])}
        expected |= {str(h).lower() for h in (manifest.get("in_scope_hosts") or [])}
        if total >= _SENSITIVE_TRIP and _host_of(final_url) not in expected:
            return {"halt": True,
                    "reason": f"unexpected access to {total} real secrets/PII in one response"}

    # 4) Rate / data-volume budgets exceeded.
    rate = manifest.get("rate") or {}
    vol = manifest.get("data_volume") or {}
    if rate.get("max_requests") and counters.get("requests", 0) > int(rate["max_requests"]):
        return {"halt": True, "reason": f"request budget exceeded ({counters.get('requests')} > {rate['max_requests']})"}
    if vol.get("max_bytes") and counters.get("bytes", 0) > int(vol["max_bytes"]):
        return {"halt": True, "reason": f"data-volume budget exceeded ({counters.get('bytes')} > {vol['max_bytes']} bytes)"}

    return {"halt": False, "reason": ""}
