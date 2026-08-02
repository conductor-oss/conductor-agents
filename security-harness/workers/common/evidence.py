"""Deterministic confirmation helpers for live exploit campaigns.

The planner and the code sandbox are intentionally untrusted evidence producers:
they can suggest a finding or print any string.  Confirmation therefore consumes
only receipts emitted by a dedicated verifier worker.  This module is deliberately
small so every promotion rule is reviewable and unit-testable.
"""

from __future__ import annotations

OOB_VERIFIER = "oob_check/v1"
INBAND_VERIFIER = "inband_check/v1"
_INBAND_ORACLES = ("ssrf_differential", "rce_arithmetic")


def oob_confirmation(receipt: object) -> tuple[bool, str]:
    """Return a confirmation only for a well-formed ``oob_check`` receipt.

    A list of hits, agent-provided finding, stdout line, or a boolean alone is not
    enough: those values can originate in the generated sandbox program.  The
    workflow must thread the result of the dedicated ``oob_check`` worker here.
    """
    if not isinstance(receipt, dict):
        return False, ""
    if receipt.get("verification") != OOB_VERIFIER:
        return False, ""
    if receipt.get("available") is not True or receipt.get("hit") is not True:
        return False, ""
    hits = receipt.get("hits")
    if not isinstance(hits, list) or not hits:
        return False, ""
    tokens = {str(hit.get("token") or "") for hit in hits if isinstance(hit, dict)}
    tokens.discard("")
    if not tokens:
        return False, ""
    return True, f"verified OOB callback for {len(tokens)} canary token(s)"


def inband_confirmation(receipt: object) -> tuple[bool, str]:
    """Return a confirmation only for a well-formed ``inband_check`` receipt.

    The receipt must be produced by the dedicated in-band verifier worker, which RE-ISSUES the
    oracle itself and measures the response -- it is NOT the sandbox's stdout, an agent claim, or a
    boolean alone (any of which the generated program could author). Two oracle kinds are accepted,
    each re-checked here against its classic false positive so a partial/forged receipt cannot pass
    merely by setting ``confirmed: true``:

      ssrf_differential - a known-BLOCKED control form was blocked AND the bypass form reached the
                          backend (a distinct, non-blocked response carrying a distinctive internal
                          ``marker``). A bare 2xx / open-redirect / generic-200 has no marker and is
                          therefore NOT a confirmation.
      rce_arithmetic    - a fresh per-attempt arithmetic ``product`` appears in the response while
                          neither operand does (guards against the payload being reflected verbatim).
    """
    if not isinstance(receipt, dict):
        return False, ""
    if receipt.get("verification") != INBAND_VERIFIER:
        return False, ""
    if receipt.get("available") is not True or receipt.get("confirmed") is not True:
        return False, ""
    oracle = receipt.get("oracle")
    if oracle not in _INBAND_ORACLES:
        return False, ""
    if oracle == "ssrf_differential":
        if receipt.get("control_blocked") is not True or receipt.get("reached_backend") is not True:
            return False, ""
        marker = receipt.get("marker")
        if not marker:
            return False, ""
        return True, f"verified SSRF differential: control blocked, bypass reached backend (marker: {marker})"
    # rce_arithmetic
    if receipt.get("product_echoed") is not True or receipt.get("operands_reflected") is not False:
        return False, ""
    return True, f"verified RCE via arithmetic echo (product {receipt.get('product')})"
