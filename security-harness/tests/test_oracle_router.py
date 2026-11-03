"""Gap 1: deterministic oracle-selection. Each vulnerability class must route to the ONE oracle that
can prove it, so an agent never plants a probe that can't promote. Pure function -> table-driven."""
from common import oracle_router as orc
from common import evidence

# (hypothesis, expected oracle). Phrased the way the hypothesizer/LLM actually writes them.
CASES = [
    ({"title": "shared built-in integration provider is overwritable cross-tenant",
      "category": "registry poison"}, "cross_tenant_write_persist"),
    ({"title": "overwrite the shared openai provider definition and read as another tenant"},
     "cross_tenant_write_persist"),
    ({"title": "IDOR on /api/workflow/{id} exposes another tenant's execution data",
      "category": "bola"}, "cross_identity_access"),
    ({"title": "read another user's secret via object-level authorization gap"},
     "cross_identity_access"),
    ({"title": "mass-assignment sets role=admin on signup", "category": "privilege escalation"},
     "auth_bypass"),
    ({"title": "JWT alg=none confusion forges an admin token"}, "auth_bypass"),
    ({"title": "login SQLi bypasses authentication"}, "auth_bypass"),
    ({"title": "revoked member still accesses the workspace after downgrade"}, "monotonic_state"),
    ({"title": "checkout completes without payment — skip the pay step",
      "category": "illegal state transition"}, "auth_state_illegal_transition"),
    ({"title": "coupon code can be redeemed twice", "category": "one-time use"}, "single_use"),
    ({"title": "race condition double-spends a single-use voucher", "category": "toctou"},
     "race_invariant"),
    ({"title": "negative quantity in cart yields a negative price"}, "value_floor"),
    ({"title": "blind SSRF via workflow HTTP task reaches internal metadata",
      "category": "ssrf"}, "ssrf_differential"),
    ({"title": "webhook template evaluation exfiltrates a secret out-of-band"}, "ssrf_differential"),
    ({"title": "SSTI in /api/prompts/test leads to RCE", "category": "template injection"},
     "rce_arithmetic"),
    ({"title": "ScriptEngine.eval sandbox escape -> remote code execution"}, "rce_arithmetic"),
    ({"title": "time-based blind SQL injection with pg_sleep"}, "time_blind_sql"),
    ({"title": "boolean-based blind SQLi in the search filter"}, "boolean_sqli"),
    ({"title": "error-based SQL injection in the order-by clause", "category": "sqli"},
     "sql_error_differential"),
    ({"title": "path traversal reads /etc/passwd via the download endpoint"},
     "path_traversal_canary"),
    # Regression (live misroute 2026-11-03): "poison null byte" is a path-traversal technique, NOT
    # registry poisoning — a bare `poison` keyword must not steal it for cross_tenant_write_persist.
    ({"title": "Path traversal on /ftp downloads server-side sensitive files (poison null byte)",
      "objective_id": "other", "target": "GET /ftp/:file"}, "path_traversal_canary"),
    # ...but QUALIFIED poison still routes to the write oracle.
    ({"title": "registry poison: overwrite the shared provider definition"},
     "cross_tenant_write_persist"),
    ({"title": "stored XSS fires in the admin's context"}, "stored_xss_victim_context"),
]


def test_each_class_routes_to_its_proving_oracle():
    for hyp, expected in CASES:
        rec = orc.recommend_oracle(hyp)
        assert rec["oracle"] == expected, f"{hyp.get('title')!r} -> {rec['oracle']} (want {expected}) " \
                                          f"matched_on={rec['matched_on']!r}"
        assert rec["confidence"] == "matched"


def test_every_routed_oracle_is_a_real_registered_oracle():
    # A router that points at an oracle the promotion gate doesn't know is worse than useless.
    known = set(evidence._INBAND_ORACLES)
    for hyp, _ in CASES:
        assert orc.recommend_oracle(hyp)["oracle"] in known
    for name in orc._PROBE_FIELDS:
        assert name in known, f"router references unknown oracle {name}"


def test_probe_fields_are_attached_and_nonempty():
    for hyp, _ in CASES:
        rec = orc.recommend_oracle(hyp)
        assert rec["probe_fields"], f"no probe fields for {rec['oracle']}"
        assert rec["probe_shape"], f"no probe shape for {rec['oracle']}"


def test_explicit_confirming_oracle_overrides_keywords():
    # If an upstream critic pins the oracle, routing honors it even against the text.
    hyp = {"title": "looks like SQLi", "confirming_oracle": "cross_identity_access"}
    rec = orc.recommend_oracle(hyp)
    assert rec["oracle"] == "cross_identity_access"
    assert rec["confidence"] == "forced"
    # ...but an INVALID pinned oracle falls through to keyword routing, never trusted blindly.
    hyp2 = {"title": "error-based SQL injection", "confirming_oracle": "not_a_real_oracle"}
    assert orc.recommend_oracle(hyp2)["oracle"] == "sql_error_differential"


def test_unmatched_falls_back_to_marker_differential_not_crash():
    rec = orc.recommend_oracle({"title": "something totally unclassifiable zzz"})
    assert rec["oracle"] == "behavioral_differential"
    assert rec["confidence"] == "default"
    # robustness: non-dict / empty inputs never raise
    assert orc.recommend_oracle(None)["oracle"] == "behavioral_differential"
    assert orc.recommend_oracle("")["oracle"] == "behavioral_differential"


def test_directive_names_oracle_and_fields():
    d = orc.oracle_directive({"title": "blind SSRF to internal metadata"})
    assert "ssrf_differential" in d
    assert "CONFIRMING ORACLE" in d
    assert "markers" in d  # a required field is surfaced to the agent


def test_specificity_write_beats_read_for_shared_poisoning():
    # "cross-tenant" + "overwrite shared" must be a WRITE oracle, never the read-only one.
    hyp = {"title": "cross-tenant: overwrite shared built-in template, victim reads it"}
    assert orc.recommend_oracle(hyp)["oracle"] == "cross_tenant_write_persist"


# Adversarial corpus (router keyword-collision audit, 2026-11-03): realistic phrasings deliberately
# loaded with technique words that bleed across classes (null byte, poison, union, sleep, token, admin,
# shared, concurrent, overflow). Guards against a future rule edit silently stealing another class.
ADVERSARIAL = {
    "cross_tenant_write_persist": [
        "overwrite shared built-in openai provider definition, read as other tenant",
        "cross-tenant write persists: tamper global template seen by all tenants",
        "registry poison: repoint provider endpoint to attacker host",
        "poison the shared integration definition registry",
    ],
    "cross_identity_access": [
        "IDOR: read another user's basket via GET /rest/basket/{id}",
        "BOLA exposes another tenant's workflow execution data",
        "horizontal access: user A reads user B's order record",
        "object-level authorization gap leaks another account's secret",
    ],
    "auth_bypass": [
        "login SQLi in email field bypasses authentication as admin",
        "JWT alg=none confusion forges an admin token",
        "mass-assignment sets role=admin on signup",
        "privilege escalation: become admin via forged claim",
    ],
    "monotonic_state": [
        "revoked admin still accesses workspace after downgrade",
        "stale session token still works after permission removal",
        "former member retains access after demotion",
    ],
    "auth_state_illegal_transition": [
        "checkout completes without payment by skipping the pay step",
        "illegal state transition: approve own request out of order",
        "workflow state machine lets you skip the approval stage",
    ],
    "single_use": [
        "one-time coupon code can be redeemed twice",
        "single-use voucher replayed for extra discount",
        "promo code reuse beyond its limit",
    ],
    "race_invariant": [
        "race condition double-spends a single-use coupon under concurrency",
        "TOCTOU: concurrent requests bypass the one-per-user limit",
        "parallel requests double-redeem the voucher",
    ],
    "value_floor": [
        "negative quantity in cart yields a negative price total",
        "integer underflow sets balance below zero and persists",
        "set price to a negative value via the basket API",
    ],
    "ssrf_differential": [
        "blind SSRF via workflow HTTP task reaches internal metadata (IMDS 169.254)",
        "webhook template evaluation exfiltrates a secret out-of-band to collaborator",
        "server-side request forgery fetches internal actuator endpoint",
    ],
    "rce_arithmetic": [
        "SSTI in prompt template leads to remote code execution",
        "ScriptEngine.eval sandbox escape -> arbitrary code execution",
        "insecure deserialization yields remote code exec",
        "expression injection evaluated server-side",
    ],
    "time_blind_sql": [
        "time-based blind sql injection using pg_sleep",
        "blind SQL via WAITFOR DELAY timing oracle",
    ],
    "boolean_sqli": [
        "boolean-based blind sqli in the search filter (true/false)",
        "inferential blind sql via boolean predicate",
    ],
    "sql_error_differential": [
        "error-based SQL injection in the order-by clause",
        "union-based sql injection dumps credentials",
        "database error leaks on broken-syntax payload",
    ],
    "path_traversal_canary": [
        "path traversal on /ftp downloads server-side files via poison null byte",
        "directory traversal reads /etc/passwd (../ sequences)",
        "LFI: local file inclusion via %00 null byte bypass of extension whitelist",
        "arbitrary file read outside the web root",
    ],
    "stored_xss_victim_context": [
        "stored XSS in feedback fires in the admin's context",
        "persistent XSS executes script in another user's session",
    ],
}


def test_adversarial_corpus_no_cross_class_collisions():
    misroutes = []
    for want, titles in ADVERSARIAL.items():
        for t in titles:
            got = orc.recommend_oracle({"title": t})
            if got["oracle"] != want:
                misroutes.append(f"want={want} got={got['oracle']} on={got['matched_on']!r} :: {t}")
    assert not misroutes, "router collisions:\n" + "\n".join(misroutes)
