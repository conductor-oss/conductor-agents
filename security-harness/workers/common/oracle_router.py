"""Deterministic oracle router — Gap 1 (oracle-selection reliability).

A hypothesis without the RIGHT confirming oracle is a dead end: the agent may do the
exploit but plant a probe that can never promote (e.g. a cross-tenant write poisoning
asserted with `cross_identity_access` read-only, or an access-control claim asserted with
a metric-only `behavioral_differential`). Picking the oracle is not a judgement call —
the vulnerability class determines the only oracle that can PROVE it. This module makes
that mapping deterministic and attaches it to every hypothesis at standup, so the agent
is told exactly which inband probe to plant and which fields it MUST populate.

`recommend_oracle(hypothesis) -> {oracle, confidence, reason, probe_fields, probe_shape}`
is a pure function of the hypothesis text (title/category/surface/technique/target/
objective_id/oracle). No LLM, no network — same input, same oracle, every run.
"""
from __future__ import annotations

import re
from typing import Any

# Required producer fields per oracle (what the agent must capture for it to promote).
# Mirrors the guards in evidence.inband_confirmation / the producers in oob.inband_check.
_PROBE_FIELDS: dict[str, list[str]] = {
    "cross_tenant_write_persist": ["attacker_write", "victim_read", "injected_marker", "control_read"],
    "cross_identity_access": ["victim", "attacker", "victim_marker", "control"],
    "auth_bypass": ["escalated", "control", "privilege_marker"],
    "monotonic_state": ["from_rank", "to_rank", "control_enforced", "observed_access"],
    "ssrf_differential": ["control", "test", "markers"],
    "sql_error_differential": ["control", "test", "markers"],
    "boolean_sqli": ["baseline", "true_resp", "false_resp"],
    "time_blind_sql": ["test_ms", "control_ms", "injected_delay_ms"],
    "rce_arithmetic": ["body", "operand_a", "operand_b"],
    "stored_xss_victim_context": ["author_identity", "victim_identity", "executed_in_victim_context"],
    "path_traversal_canary": ["test_body", "canary", "control_body"],
    "race_invariant": ["allowed_successes", "observed_successes", "control_enforced"],
    "single_use": ["allowed_uses", "observed_successes", "control_enforced"],
    "value_floor": ["observed_value", "floor", "persisted"],
    "auth_state_illegal_transition": ["illegal_persisted", "control_enforced"],
    "behavioral_differential": ["control", "test", "marker"],
}

_PROBE_SHAPE: dict[str, str] = {
    "cross_tenant_write_persist":
        "As identity A write a unique marker into a SHARED/built-in object; as identity B read the "
        "same object and capture the body; control_read = B's read BEFORE the write. Confirms only if "
        "B's read contains A's marker (shared state crossed the tenant boundary).",
    "cross_identity_access":
        "victim = a resource owned by identity B carrying a marker only B can produce; attacker = A's "
        "read of that same resource; control = A reading A's OWN resource. Confirms only if A's read of "
        "B's resource contains B's marker. NEVER assert this from reflected own-input.",
    "auth_bypass":
        "escalated = response/claims after the bypass (forged/mass-assigned role, SQLi login, altered "
        "JWT); control = the same request as a legitimately-unprivileged identity; privilege_marker = a "
        "string that appears ONLY when truly privileged (admin-only field/route/record).",
    "monotonic_state":
        "from_rank/to_rank = numeric privilege levels; observed_access proves the lower rank still "
        "performed the higher-rank action after a downgrade; control_enforced = the downgrade is "
        "supposed to block it.",
    "ssrf_differential":
        "test = response when the server is coerced to fetch an attacker/internal target; control = a "
        "benign target; markers = tokens that appear ONLY on the internal/OOB hit (OOB callback token, "
        "IMDS/actuator body). For blind SSRF/exfil use the OOB collaborator token as the marker.",
    "sql_error_differential":
        "test = response to a syntactically-broken injection; control = benign input; markers = DB "
        "error signatures that surface only when the payload reaches SQL.",
    "boolean_sqli":
        "baseline = normal response; true_resp = response to an always-true predicate; false_resp = "
        "always-false. Confirms only if true matches baseline and false diverges.",
    "time_blind_sql":
        "test_ms = latency with an injected SLEEP/delay; control_ms = latency without; injected_delay_ms "
        "= the delay you asked for. Confirms only if the observed delta tracks the injection.",
    "rce_arithmetic":
        "Inject an arithmetic expression (operand_a, operand_b) into the sink; body = the response. "
        "Confirms only if the body contains the computed product (proves server-side evaluation).",
    "stored_xss_victim_context":
        "author_identity stores the payload; victim_identity renders it; executed_in_victim_context must "
        "be proven true (payload ran in B's authenticated context, not the author's).",
    "path_traversal_canary":
        "test_body = response to a traversal path; canary = a string from a file outside the web root "
        "the app must not serve; control_body = response to a benign path.",
    "race_invariant":
        "allowed_successes = the invariant ceiling (e.g. 1 redemption); observed_successes = how many "
        "concurrent requests succeeded; control_enforced = the single-request path is rejected.",
    "single_use":
        "allowed_uses = declared limit (usually 1); observed_successes = successful reuses; "
        "control_enforced = the second use is supposed to fail.",
    "value_floor":
        "observed_value = the value that landed server-side after submitting an illegal one (negative "
        "price/quantity/balance); floor = the legal minimum; persisted = the illegal value stuck.",
    "auth_state_illegal_transition":
        "illegal_persisted = an out-of-order/skipped-step state the server accepted and kept; "
        "control_enforced = the legitimate flow rejects that transition.",
    "behavioral_differential":
        "LEAD ONLY for access-control. control/test responses plus a cross-boundary marker. A bare "
        "length/metric delta does NOT promote — only a genuine marker divergence does. Prefer a specific "
        "oracle above; use this only when no structural oracle fits.",
}

# Ordered (specific -> general). First rule whose pattern hits the blob wins.
# Each: (oracle, compiled_pattern, reason).
_RULES: list[tuple[str, "re.Pattern[str]", str]] = []


def _rule(oracle: str, pattern: str, reason: str) -> None:
    _RULES.append((oracle, re.compile(pattern, re.I), reason))


# --- shared-state / tenant-boundary WRITES (most-specific first) ---
# NOTE: `poison` must stay QUALIFIED (registry/shared/provider/tenant). A bare `poison` greedily steals
# path-traversal's "poison null byte" and cache/DNS poisoning — different classes, different oracles.
_rule("cross_tenant_write_persist",
      r"cross.?tenant|shared (built.?in|registry|definition|provider|object|resource|config)|"
      r"(registry|provider|definition|template|config|integration|global|shared|tenant)[ -]?poison|"
      r"poison[ -]?(the )?(registry|provider|definition|template|config|integration|global|shared)|"
      r"overwrite .*(shared|built.?in|provider|definition|template|global)|"
      r"repoint|tamper .*(shared|global)|write.*persist.*other tenant",
      "write-to-shared-then-read-as-victim is the only oracle that proves a persisted cross-tenant "
      "write; a read-only oracle cannot show the write crossed the boundary")

# --- horizontal / object-level access (reads of another identity's data) ---
_rule("cross_identity_access",
      r"\bidor\b|\bbola\b|horizontal|another (tenant|user|identity|account)'?s? (data|resource|record|"
      r"workflow|execution|secret|object)|cross.?identity|read .*other (tenant|user)|"
      r"object.level author|unauthor.*read|access .*other.*resource",
      "reading victim B's resource as attacker A requires a real victim-marker comparison, not a "
      "reflected-input or metric check")

# --- privilege regression / downgrade still-works ---
_rule("monotonic_state",
      r"downgrade|revoke.*still|after (removal|demotion|downgrade)|monotonic|stale (role|permission|"
      r"session|token).*still|privilege.*regress|former (admin|member).*access",
      "a privilege that should have dropped but still works is a monotonicity breach, proven by "
      "lower-rank-still-acts, not a one-shot read")

# --- authn/authz bypass / privilege escalation / mass-assignment ---
_rule("auth_bypass",
      r"\bauth(n|z)? bypass|authentication bypass|privilege escalat|priv.?esc|mass.?assign|"
      r"role\s*[=:]\s*admin|\brole=admin|become admin|forge(d)? (jwt|token|claim)|jwt (alg|none|"
      r"confusion|forg)|login (sqli|bypass)|admin-?only|elevate",
      "escalation must be proven by a privilege_marker that only a truly-privileged principal can see, "
      "against an unprivileged control")

# --- illegal state transitions / broken business-flow ordering ---
_rule("auth_state_illegal_transition",
      r"illegal (state|transition)|out.?of.?order|skip (step|stage|payment|approval)|"
      r"state machine|workflow state.*(skip|bypass)|checkout.*without pay|approve.*own",
      "an out-of-order state the server accepts and keeps is proven by persisted-illegal-transition")

# --- race / TOCTOU / concurrency (ranks above single_use: a RACE double-spend is proven by
#     concurrent-successes-exceed-ceiling, even when the thing raced is a single-use coupon) ---
_rule("race_invariant",
      r"\brace\b|toctou|time.?of.?check|concurren|parallel request|double.?spend|"
      r"simultaneous|limit.*bypass.*concurrent",
      "a concurrency breach needs observed_successes > allowed_successes against an enforced control")

# --- one-time / single-use invariant (sequential reuse, no concurrency) ---
_rule("single_use",
      r"one.?time|single.?use|coupon|voucher|promo|redeem|reuse .*(code|token|coupon|ticket)|"
      r"use .*twice|replay .*(coupon|voucher|otp|nonce)",
      "a code that should work once but works again is a single-use invariant breach")

# --- numeric floor / negative value / underflow ---
_rule("value_floor",
      r"negative (price|amount|quantity|balance|value)|underflow|value floor|below (zero|minimum)|"
      r"price manipulat|quantity.*(negative|overflow)|refund.*more|set .*(price|balance).*to",
      "an illegal value that persists server-side is proven by value_floor (observed < floor, persisted)")

# --- SSRF / blind exfil / OOB callback / webhook secret exfil ---
_rule("ssrf_differential",
      r"\bssrf\b|server.?side request|out.?of.?band|\boob\b|blind (callback|exfil|fetch)|"
      r"exfiltrat|dns (callback|lookup)|collaborator|internal (metadata|service|endpoint|network)|"
      r"imds|169\.254|actuator|fetch .*(internal|localhost|\[::1\])|webhook.*(secret|template|exfil)|"
      r"template.*secret|secret.*exfil",
      "reach/exfil is proven by a marker that appears ONLY on the internal/OOB hit (use the OOB "
      "collaborator token for blind cases)")

# --- RCE / code / expression / template evaluation ---
_rule("rce_arithmetic",
      r"\brce\b|remote code|code (injection|exec)|command injection|\bssti\b|template injection|"
      r"expression (injection|eval)|eval\(|scriptengine|deseriali[sz]|compile .*(payload|code)|"
      r"arbitrary code",
      "server-side evaluation is proven by making the server compute an arithmetic product you chose "
      "and reading it back (no interpretation needed)")

# --- SQLi variants ---
_rule("time_blind_sql",
      r"time.?based (sql|blind)|blind sql.*time|sleep\(|waitfor delay|benchmark\(|pg_sleep",
      "time-based SQLi is proven by a latency delta that tracks the injected delay")
_rule("boolean_sqli",
      r"boolean.?based|blind sql(i)?\b|inferential sql|true/false.*sql",
      "boolean SQLi is proven by true-predicate==baseline and false-predicate!=baseline")
_rule("sql_error_differential",
      r"\bsqli?\b|sql injection|error.?based sql|union.?based|database error",
      "SQLi is proven by DB error signatures surfacing only on the broken-syntax payload")

# --- path traversal / LFI ---
_rule("path_traversal_canary",
      r"path traversal|directory traversal|\blfi\b|local file (inclusion|read)|\.\./|arbitrary file read|"
      r"read .*(/etc/passwd|file outside)|null[ -]?byte|%00|poison null byte",
      "file read outside the web root is proven by a canary string the app must not serve")

# --- stored XSS ---
_rule("stored_xss_victim_context",
      r"stored xss|persistent xss|xss.*(stored|victim|admin context)|script.*executes.*(victim|another)",
      "stored XSS is proven by execution in the VICTIM's authenticated context, not the author's")

_DEFAULT = ("behavioral_differential",
            "no structural oracle matched the hypothesis text; fall back to a marker-bearing "
            "differential (metric-only will NOT promote — capture a real cross-boundary marker)")


def _blob(h: Any) -> str:
    if not isinstance(h, dict):
        return str(h or "")
    parts = [str(h.get(k) or "") for k in
             ("title", "objective_id", "category", "surface", "technique", "target",
              "oracle", "test_plan", "hypothesis", "description", "class", "family")]
    return " ".join(parts)


def recommend_oracle(hypothesis: Any) -> dict:
    """Return the deterministic confirming-oracle recommendation for a hypothesis.

    {oracle, confidence ('matched'|'default'), reason, probe_fields, probe_shape, matched_on}.
    Pure: no LLM, no network. If the hypothesis already carries a VALID oracle name in
    `confirming_oracle`, that wins (an upstream critic/LLM may set it explicitly).
    """
    forced = ""
    if isinstance(hypothesis, dict):
        forced = str(hypothesis.get("confirming_oracle") or "").strip()
    if forced in _PROBE_FIELDS:
        return {"oracle": forced, "confidence": "forced",
                "reason": "explicitly set on the hypothesis",
                "probe_fields": _PROBE_FIELDS[forced], "probe_shape": _PROBE_SHAPE.get(forced, ""),
                "matched_on": "confirming_oracle"}

    blob = _blob(hypothesis)
    for oracle, pat, reason in _RULES:
        m = pat.search(blob)
        if m:
            return {"oracle": oracle, "confidence": "matched", "reason": reason,
                    "probe_fields": _PROBE_FIELDS[oracle], "probe_shape": _PROBE_SHAPE.get(oracle, ""),
                    "matched_on": m.group(0).strip().lower()}

    oracle, reason = _DEFAULT
    return {"oracle": oracle, "confidence": "default", "reason": reason,
            "probe_fields": _PROBE_FIELDS[oracle], "probe_shape": _PROBE_SHAPE.get(oracle, ""),
            "matched_on": ""}


def oracle_directive(hypothesis: Any, rec: dict | None = None) -> str:
    """One-line directive to splice into an agent's focus_directive. Pass a precomputed `rec` (from
    recommend_oracle on the RAW hypothesis) to keep the keyword-match reason instead of re-deriving it."""
    r = rec if isinstance(rec, dict) and rec.get("oracle") else recommend_oracle(hypothesis)
    return (f"CONFIRMING ORACLE = `{r['oracle']}` ({r['reason']}). "
            f"Plant this probe: {r['probe_shape']} "
            f"Required fields: {', '.join(r['probe_fields'])}. "
            f"A confirmation with any OTHER oracle (or missing these fields) will NOT promote.")
