"""Deterministic in-band exploitation oracles (PLAN_V3 section 5.4).

These are the proof functions a dedicated in-band verifier worker calls AFTER it re-issues an
oracle against the target itself (never trusting sandbox stdout). Each returns the typed proof
fields that `evidence.inband_confirmation` re-validates before a finding is promoted, and each
encodes the guard against its classic false positive so a lucky/benign response cannot confirm.

Pure logic, unit-tested (`tests/test_inband_oracle.py`). A verifier worker wraps a result as::

    {"verification": evidence.INBAND_VERIFIER, "available": True, **result}
"""

from __future__ import annotations

# Statuses a front-end/authz layer returns when it BLOCKS a request (used for the SSRF control).
DEFAULT_BLOCKED_STATUSES = (401, 403, 407)


def _contains(haystack, needle) -> bool:
    return bool(needle) and str(needle) in str(haystack or "")


def ssrf_differential(control: dict, test: dict, markers,
                      blocked_statuses=DEFAULT_BLOCKED_STATUSES) -> dict:
    """Confirm a server-side request forgery by DIFFERENTIAL, not by a bare success.

    ``control`` is the response to a known-blocked internal form (e.g. ``http://127.0.0.1/...``);
    ``test`` is the response to a candidate bypass form (e.g. ``http://[::1]/...``). Each is a dict
    ``{status, body}``. ``markers`` are distinctive strings that only an INTERNAL backend response
    would contain (e.g. an internal-only path, an actuator key, an OpenAPI title).

    Confirmed iff: the control was blocked, the test was NOT blocked, and at least one marker is
    present in the test body but ABSENT from the control body (so an open redirect, a generic 200
    error page, or a marker the front-end echoes for everyone cannot pass).
    """
    control = control or {}
    test = test or {}
    markers = [m for m in (markers or []) if m]
    cstatus, tstatus = control.get("status"), test.get("status")
    cbody, tbody = control.get("body"), test.get("body")

    control_blocked = cstatus in blocked_statuses
    test_not_blocked = tstatus is not None and tstatus not in blocked_statuses
    marker = next((m for m in markers if _contains(tbody, m) and not _contains(cbody, m)), "")
    reached_backend = bool(test_not_blocked and marker)
    return {
        "oracle": "ssrf_differential",
        "confirmed": bool(control_blocked and reached_backend),
        "control_blocked": control_blocked,
        "reached_backend": reached_backend,
        "marker": marker,
        "control_status": cstatus,
        "test_status": tstatus,
    }


def rce_arithmetic(body, operand_a: int, operand_b: int) -> dict:
    """Confirm code/expression execution by an arithmetic echo the target had to COMPUTE.

    The verifier injects an expression like ``<a>*<b>`` with fresh per-attempt operands and passes
    the response ``body`` here. Confirmed iff the product appears in the response AND neither operand
    does -- if an operand appears, the payload was reflected verbatim rather than evaluated, so it is
    NOT confirmation. Operands should be large/random so the product cannot occur by chance.
    """
    product = int(operand_a) * int(operand_b)
    product_echoed = _contains(body, product)
    operands_reflected = _contains(body, operand_a) or _contains(body, operand_b)
    return {
        "oracle": "rce_arithmetic",
        "confirmed": bool(product_echoed and not operands_reflected),
        "product": product,
        "product_echoed": product_echoed,
        "operands_reflected": operands_reflected,
    }
