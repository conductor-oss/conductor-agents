"""Deterministic confirmation helpers for live exploit campaigns.

The planner and the code sandbox are intentionally untrusted evidence producers:
they can suggest a finding or print any string.  Confirmation therefore consumes
only receipts emitted by a dedicated verifier worker.  This module is deliberately
small so every promotion rule is reviewable and unit-testable.
"""

from __future__ import annotations

OOB_VERIFIER = "oob_check/v1"
INBAND_VERIFIER = "inband_check/v1"
_INBAND_ORACLES = ("ssrf_differential", "rce_arithmetic", "time_blind_sql",
                   "stored_xss_victim_context", "race_invariant", "path_traversal_canary",
                   "auth_state_illegal_transition", "sql_error_differential", "boolean_sqli",
                   "cross_identity_access", "auth_bypass", "behavioral_differential",
                   "single_use", "value_floor", "monotonic_state", "cross_tenant_write_persist")


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
        # The proof is an internal-only marker present in the test and absent from the control
        # (reached_backend), under EITHER model: egress-bypass (control_blocked) or
        # direct-reach-reflection (control is a benign external target, not blocked). Re-check the
        # exclusive marker; control_blocked is corroboration, not required.
        if receipt.get("reached_backend") is not True:
            return False, ""
        marker = receipt.get("marker")
        if not marker:
            return False, ""
        model = receipt.get("model") or ("egress_bypass" if receipt.get("control_blocked") else "direct_reach_reflection")
        return True, f"verified SSRF differential ({model}): internal marker reached only via the test (marker: {marker})"
    if oracle == "rce_arithmetic":
        if receipt.get("product_echoed") is not True or receipt.get("operands_reflected") is not False:
            return False, ""
        return True, f"verified RCE via arithmetic echo (product {receipt.get('product')})"
    if oracle == "time_blind_sql":
        if receipt.get("control_fast") is not True:
            return False, ""
        try:
            delta = float(receipt.get("delta_ms")); thr = float(receipt.get("threshold_ms"))
        except (TypeError, ValueError):
            return False, ""
        if thr <= 0 or delta < thr:
            return False, ""
        return True, f"verified blind SQLi via conditional time delta ({delta}ms >= {thr}ms)"
    if oracle == "stored_xss_victim_context":
        if receipt.get("executed_in_victim_context") is not True or receipt.get("cross_identity") is not True:
            return False, ""
        return True, "verified stored XSS executing in a victim identity's context"
    if oracle == "race_invariant":
        if receipt.get("invariant_breached") is not True or receipt.get("control_enforced") is not True:
            return False, ""
        return True, (f"verified race condition: {receipt.get('observed_successes')} successes exceed "
                      f"allowed {receipt.get('allowed_successes')} with the sequential rule enforced")
    if oracle == "path_traversal_canary":
        if (receipt.get("canary_present") is not True or receipt.get("control_clean") is not True
                or not receipt.get("marker")):
            return False, ""
        return True, f"verified path traversal via file-content canary ({receipt.get('marker')})"
    if oracle == "auth_state_illegal_transition":
        if receipt.get("illegal_persisted") is not True or receipt.get("control_enforced") is not True:
            return False, ""
        return True, "verified illegal auth-state transition persisted past an enforced control"
    if oracle == "sql_error_differential":
        if not receipt.get("marker"):
            return False, ""
        return True, f"verified error-based SQLi: DB error marker '{receipt.get('marker')}' only on the injection"
    if oracle == "boolean_sqli":
        if receipt.get("true_matches_baseline") is not True or receipt.get("false_differs") is not True:
            return False, ""
        return True, "verified boolean-blind SQLi: AND 1=1 matches baseline, AND 1=2 diverges"
    if oracle == "cross_identity_access":
        if receipt.get("marker_leaked") is not True or receipt.get("control_clean") is not True:
            return False, ""
        return True, "verified cross-identity access (IDOR/BOLA/broken-access-control): a different identity obtained the victim's data"
    if oracle == "auth_bypass":
        if receipt.get("marker_leaked") is not True or receipt.get("control_clean") is not True:
            return False, ""
        return True, f"verified authentication/authorization bypass: an identity obtained access the legitimate path denies (marker: {receipt.get('marker')})"
    if oracle == "behavioral_differential":
        if receipt.get("has_signal") is not True or receipt.get("replayed") is not True:
            return False, ""
        return True, "verified behavioral differential: an attacker-controlled discriminator produced a reproducible control-vs-test divergence"
    if oracle == "single_use":
        if receipt.get("invariant_breached") is not True or receipt.get("control_enforced") is not True:
            return False, ""
        return True, (f"verified single-use bypass: {receipt.get('observed_successes')} redemptions exceed "
                      f"allowed {receipt.get('allowed_uses')} with the limit enforced sequentially")
    if oracle == "value_floor":
        if receipt.get("below_floor") is not True or receipt.get("persisted") is not True:
            return False, ""
        return True, f"verified value-floor violation: an illegal value {receipt.get('observed_value')} below {receipt.get('floor')} was accepted and persisted"
    if oracle == "monotonic_state":
        if receipt.get("regressed") is not True or receipt.get("control_enforced") is not True:
            return False, ""
        return True, "verified illegal state regression on a forward-only workflow past an enforced guard"
    if oracle == "cross_tenant_write_persist":
        if (receipt.get("write_accepted") is not True or receipt.get("marker_persisted") is not True
                or receipt.get("control_clean") is not True):
            return False, ""
        return True, (f"verified shared-resource cross-tenant poisoning: one tenant's write (marker "
                      f"'{receipt.get('marker')}') persisted into a shared resource visible to a different tenant")
    return False, ""
