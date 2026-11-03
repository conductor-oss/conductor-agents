"""In-band verifier trust core (PLAN_V3 5.4, item #3): oracle math + receipt gate + deepen
promotion, incl. the false-positive guards and the no-self-confirmation invariant."""
from common import deepen, evidence
from common import inband_oracle as io


# --- SSRF differential oracle ---

def test_ssrf_confirms_on_blocked_control_and_marked_bypass():
    r = io.ssrf_differential(
        control={"status": 403, "body": "blocked"},
        test={"status": 200, "body": "{\"openapi\":\"3.0\",\"title\":\"internal-admin\"}"},
        markers=["internal-admin"])
    assert r["confirmed"] is True and r["marker"] == "internal-admin"


def test_ssrf_bare_2xx_without_marker_is_not_confirmed():
    r = io.ssrf_differential(control={"status": 403, "body": "no"},
                             test={"status": 200, "body": "generic ok page"}, markers=["internal-admin"])
    assert r["confirmed"] is False and r["reached_backend"] is False


def test_ssrf_marker_present_in_control_too_is_not_confirmed():
    # Front-end echoes the marker for everyone -> not a differential.
    r = io.ssrf_differential(control={"status": 403, "body": "internal-admin banner"},
                             test={"status": 200, "body": "internal-admin banner"},
                             markers=["internal-admin"])
    assert r["confirmed"] is False


def test_ssrf_direct_reach_reflection_confirms_without_blocked_control():
    # DIRECT-REACH model (config-repoint SSRF, observed live on orkescloud): control and test hit the
    # SAME endpoint pointed at a benign vs an INTERNAL target; the internal backend's body is reflected
    # only in the test (e.g. the app's own INVALID_TOKEN), both non-blocked. The exclusive internal
    # marker is proof the server made the request -- no blocked control required.
    r = io.ssrf_differential(
        control={"status": 500, "body": "500 ... for https://api.openai.com/openai/models: 404"},
        test={"status": 500, "body": "500 ... for http://127.0.0.1:8080/openai/models: {\"error\":\"INVALID_TOKEN\"}"},
        markers=["INVALID_TOKEN"])
    assert r["confirmed"] is True and r["marker"] == "INVALID_TOKEN"
    assert r["model"] == "direct_reach_reflection" and r["control_blocked"] is False


def test_ssrf_direct_reach_needs_exclusive_marker():
    # Same two non-blocked 500s but the marker is in BOTH (front-end echo) -> NOT confirmed.
    r = io.ssrf_differential(control={"status": 500, "body": "err INVALID_TOKEN"},
                             test={"status": 500, "body": "err INVALID_TOKEN"}, markers=["INVALID_TOKEN"])
    assert r["confirmed"] is False


# --- RCE arithmetic oracle ---

def test_rce_confirms_when_product_echoed_and_operands_absent():
    r = io.rce_arithmetic(body="result is 1787569 done", operand_a=1337, operand_b=1337)
    assert r["confirmed"] is True and r["product"] == 1787569


def test_rce_reflected_operand_is_not_confirmed():
    # The payload "1337*1337" reflected verbatim: operand present -> reflection, not evaluation.
    r = io.rce_arithmetic(body="you sent 1337*1337 and 1787569", operand_a=1337, operand_b=1337)
    assert r["confirmed"] is False and r["operands_reflected"] is True


# --- receipt gate (evidence.inband_confirmation) ---

def _ssrf_receipt(**over):
    r = {"verification": evidence.INBAND_VERIFIER, "available": True, "oracle": "ssrf_differential",
         "confirmed": True, "control_blocked": True, "reached_backend": True, "marker": "internal-admin"}
    r.update(over)
    return r


def test_gate_accepts_wellformed_receipt():
    ok, ev = evidence.inband_confirmation(_ssrf_receipt())
    assert ok is True and "SSRF differential" in ev


def test_gate_rejects_forged_confirmed_without_proof():
    # confirmed:true but the proof fields are missing -> rejected.
    ok, _ = evidence.inband_confirmation(_ssrf_receipt(marker="", reached_backend=False))
    assert ok is False


def test_gate_rejects_wrong_verification_string():
    ok, _ = evidence.inband_confirmation(_ssrf_receipt(verification="sandbox_says_so"))
    assert ok is False


def test_gate_rejects_non_dict():
    assert evidence.inband_confirmation("confirmed!") == (False, "")


# --- deepen promotion + the no-self-confirmation invariant ---

_STATE = {"sink_class": "ssrf", "ladder": ["oob-confirm"], "ladder_detail": [{"family": "oob-confirm"}]}


def test_detect_confirmation_promotes_on_inband_receipt():
    ok, ev = deepen.detect_confirmation(inband_verification=_ssrf_receipt())
    assert ok is True and "marker" in ev


def test_observe_promotes_on_inband_receipt():
    new = deepen.observe(dict(_STATE), "oob-confirm", "tried [::1] bypass",
                         inband_verification=_ssrf_receipt())
    assert new["confirmed"] is True
    assert new["ledger"]["oob-confirm"]["outcome"] == "confirmed"


def test_observe_never_confirms_from_sandbox_stdout():
    # A rich but UNTRUSTED sandbox result must never confirm -- only verifier receipts do.
    new = deepen.observe(dict(_STATE), "oob-confirm", "x",
                         result={"evidence": ["looks confirmed!"], "findings": [{"x": 1}],
                                 "oob": [{"token": "t"}]})
    assert new.get("confirmed") is not True


# --- time-blind SQLi oracle ---

def test_time_blind_confirms_on_conditional_delta():
    r = io.time_blind_sql(test_ms=5200, control_ms=120, injected_delay_ms=5000)
    assert r["confirmed"] is True and r["delta_ms"] >= r["threshold_ms"]


def test_time_blind_uniformly_slow_endpoint_not_confirmed():
    # Both the test and the control are slow -> no conditional differential.
    r = io.time_blind_sql(test_ms=5200, control_ms=5000, injected_delay_ms=5000)
    assert r["confirmed"] is False


def test_time_blind_jitter_below_threshold_not_confirmed():
    r = io.time_blind_sql(test_ms=800, control_ms=120, injected_delay_ms=5000)
    assert r["confirmed"] is False


# --- stored-XSS victim-context oracle ---

def test_stored_xss_cross_identity_execution_confirms():
    r = io.stored_xss_victim_context(author_identity="userA", victim_identity="admin",
                                     executed_in_victim_context=True, marker="cnry1")
    assert r["confirmed"] is True and r["cross_identity"] is True


def test_stored_xss_same_identity_reflection_not_confirmed():
    r = io.stored_xss_victim_context(author_identity="userA", victim_identity="userA",
                                     executed_in_victim_context=True)
    assert r["confirmed"] is False and r["cross_identity"] is False


def test_stored_xss_persisted_but_not_executed_not_confirmed():
    r = io.stored_xss_victim_context(author_identity="userA", victim_identity="admin",
                                     executed_in_victim_context=False)
    assert r["confirmed"] is False


# --- race-invariant oracle ---

def test_race_confirms_when_burst_breaks_enforced_rule():
    r = io.race_invariant(allowed_successes=1, observed_successes=4, control_enforced=True)
    assert r["confirmed"] is True and r["invariant_breached"] is True


def test_race_no_limit_at_all_is_not_a_race_confirmation():
    # Sequential control did NOT enforce the rule -> it's a missing-limit finding, not a race.
    r = io.race_invariant(allowed_successes=1, observed_successes=5, control_enforced=False)
    assert r["confirmed"] is False


def test_race_within_allowance_not_confirmed():
    r = io.race_invariant(allowed_successes=3, observed_successes=2, control_enforced=True)
    assert r["confirmed"] is False


# --- path-traversal canary oracle ---

def test_traversal_confirms_on_file_canary_absent_from_control():
    r = io.path_traversal_canary(test_body="root:x:0:0:root:/root:/bin/bash",
                                 canary="root:x:0:0", control_body="not found")
    assert r["confirmed"] is True and r["marker"] == "root:x:0:0"


def test_traversal_canary_in_control_too_not_confirmed():
    r = io.path_traversal_canary(test_body="root:x:0:0", canary="root:x:0:0",
                                 control_body="root:x:0:0 always here")
    assert r["confirmed"] is False and r["control_clean"] is False


# --- auth-state illegal-transition oracle ---

def test_auth_state_confirms_when_illegal_persists_past_enforced_control():
    r = io.auth_state_illegal_transition(illegal_persisted=True, control_enforced=True,
                                         from_state="shipped", to_state="refunded")
    assert r["confirmed"] is True


def test_auth_state_no_guard_at_all_not_confirmed():
    r = io.auth_state_illegal_transition(illegal_persisted=True, control_enforced=False)
    assert r["confirmed"] is False


# --- receipt gate accepts/rejects each new oracle ---

def _receipt(oracle, **fields):
    r = {"verification": evidence.INBAND_VERIFIER, "available": True, "oracle": oracle, "confirmed": True}
    r.update(fields)
    return r


def test_gate_accepts_each_new_oracle():
    cases = [
        _receipt("time_blind_sql", control_fast=True, delta_ms=3100.0, threshold_ms=3000.0),
        _receipt("stored_xss_victim_context", executed_in_victim_context=True, cross_identity=True),
        _receipt("race_invariant", invariant_breached=True, control_enforced=True,
                 observed_successes=4, allowed_successes=1),
        _receipt("path_traversal_canary", canary_present=True, control_clean=True, marker="root:x:0:0"),
        _receipt("auth_state_illegal_transition", illegal_persisted=True, control_enforced=True),
    ]
    for rec in cases:
        ok, ev = evidence.inband_confirmation(rec)
        assert ok is True and ev, f"{rec['oracle']} should pass the gate"


def test_gate_rejects_forged_new_oracle_receipts():
    forged = [
        _receipt("time_blind_sql", control_fast=False, delta_ms=9999.0, threshold_ms=3000.0),
        _receipt("time_blind_sql", control_fast=True, delta_ms=10.0, threshold_ms=3000.0),
        _receipt("stored_xss_victim_context", executed_in_victim_context=True, cross_identity=False),
        _receipt("race_invariant", invariant_breached=True, control_enforced=False),
        _receipt("path_traversal_canary", canary_present=True, control_clean=False, marker=""),
        _receipt("auth_state_illegal_transition", illegal_persisted=True, control_enforced=False),
    ]
    for rec in forged:
        ok, _ = evidence.inband_confirmation(rec)
        assert ok is False, f"{rec['oracle']} forged receipt must be rejected"


# --- error-based + boolean SQLi oracles (Juice Shop class) ---

def test_sql_error_differential_confirms_on_db_error():
    r = io.sql_error_differential(control={"status": 200, "body": "[{\"id\":1}]"},
                                  test={"status": 500, "body": "SQLITE_ERROR: unrecognized token near \"'\""})
    assert r["confirmed"] is True and r["marker"]


def test_sql_error_differential_rejects_when_marker_in_control_too():
    r = io.sql_error_differential(control={"status": 500, "body": "sqlite error banner always shown"},
                                  test={"status": 500, "body": "sqlite error banner always shown"})
    assert r["confirmed"] is False


def test_boolean_sqli_confirms_on_predicate_diff():
    r = io.boolean_sqli(baseline={"rows": 3}, true_resp={"rows": 3}, false_resp={"rows": 0})
    assert r["confirmed"] is True


def test_boolean_sqli_rejects_when_false_matches():
    r = io.boolean_sqli(baseline={"rows": 3}, true_resp={"rows": 3}, false_resp={"rows": 3})
    assert r["confirmed"] is False


def test_gate_accepts_sqli_receipts():
    ok1, e1 = evidence.inband_confirmation({"verification": evidence.INBAND_VERIFIER, "available": True,
        "oracle": "sql_error_differential", "confirmed": True, "marker": "sqlite"})
    ok2, e2 = evidence.inband_confirmation({"verification": evidence.INBAND_VERIFIER, "available": True,
        "oracle": "boolean_sqli", "confirmed": True, "true_matches_baseline": True, "false_differs": True})
    assert ok1 and ok2
    # forged: no marker / false didn't differ
    assert evidence.inband_confirmation({"verification": evidence.INBAND_VERIFIER, "available": True,
        "oracle": "sql_error_differential", "confirmed": True, "marker": ""})[0] is False
    assert evidence.inband_confirmation({"verification": evidence.INBAND_VERIFIER, "available": True,
        "oracle": "boolean_sqli", "confirmed": True, "true_matches_baseline": True, "false_differs": False})[0] is False


# --- cross-identity access (IDOR/BOLA/broken-access-control) ---

def test_cross_identity_confirms_on_victim_data_leak():
    r = io.cross_identity_access(
        victim={"status": 200, "body": "bob@x.io order#42"},
        attacker={"status": 200, "body": "order for bob@x.io"},
        victim_marker="bob@x.io",
        control={"status": 200, "body": "order for alice@x.io"})
    assert r["confirmed"] is True and r["marker_leaked"] is True


def test_cross_identity_rejects_blocked_attacker():
    r = io.cross_identity_access(victim={"status": 200, "body": "bob@x.io"},
                                attacker={"status": 403, "body": "forbidden"}, victim_marker="bob@x.io")
    assert r["confirmed"] is False


def test_cross_identity_rejects_common_field():
    r = io.cross_identity_access(victim={"status": 200, "body": "shared"},
                                attacker={"status": 200, "body": "shared"}, victim_marker="shared",
                                control={"status": 200, "body": "shared"})
    assert r["confirmed"] is False  # marker present in attacker's own control -> not a leak


def test_gate_accepts_cross_identity_receipt():
    ok, ev = evidence.inband_confirmation({"verification": evidence.INBAND_VERIFIER, "available": True,
        "oracle": "cross_identity_access", "confirmed": True, "marker_leaked": True, "control_clean": True})
    assert ok and "IDOR" in ev


# === swarm-novel-discovery Workstream 1: new oracles ===

def _wrap(**fields):
    return {"verification": evidence.INBAND_VERIFIER, "available": True, **fields}


# --- auth_bypass ---

def test_auth_bypass_confirms_login_sqli_to_admin():
    # Injected login returns an admin session; the wrong-password control is rejected and clean.
    r = io.auth_bypass(
        escalated={"status": 200, "body": '{"authentication":{"umail":"admin@juice-sh.op","token":"x"}}'},
        control={"status": 401, "body": "Invalid email or password."},
        privilege_marker="admin@juice-sh.op")
    assert r["confirmed"] is True and r["marker"] == "admin@juice-sh.op"
    ok, _ = evidence.inband_confirmation(_wrap(**r))
    assert ok is True


def test_auth_bypass_rejects_marker_present_in_control():
    # A field the control also returns is not proof of elevation.
    r = io.auth_bypass(escalated={"status": 200, "body": "role=admin"},
                       control={"status": 200, "body": "role=admin"},
                       privilege_marker="role=admin")
    assert r["confirmed"] is False
    assert evidence.inband_confirmation(_wrap(**r))[0] is False


def test_auth_bypass_rejects_bare_2xx_no_marker():
    r = io.auth_bypass(escalated={"status": 200, "body": "ok"}, control={"status": 401, "body": "no"},
                       privilege_marker="admin@juice-sh.op")
    assert r["confirmed"] is False


# --- behavioral_differential ---

def test_behavioral_differential_confirms_replayed_marker():
    r = io.behavioral_differential(control={"status": 200, "body": "normal"},
                                   test={"status": 200, "body": "SECRET_LEAK normal"},
                                   marker="SECRET_LEAK", replayed=True)
    assert r["confirmed"] is True and r["marker_divergence"] is True
    assert evidence.inband_confirmation(_wrap(**r))[0] is True


def test_behavioral_differential_rejects_unreplayed():
    r = io.behavioral_differential(control={"body": "a"}, test={"body": "SECRET a"},
                                   marker="SECRET", replayed=False)
    assert r["confirmed"] is False
    assert evidence.inband_confirmation(_wrap(**r))[0] is False


def test_behavioral_differential_metric_only_is_lead_not_confirmation():
    # A bare length/metric delta (different inputs -> different-sized responses) is NOT proof:
    # it must NOT confirm, only flag a lead. This is the real-API false-positive guard.
    r = io.behavioral_differential(control={"body": "x"}, test={"body": "y"},
                                   control_metric=10, test_metric=100, min_delta=50, replayed=True)
    assert r["confirmed"] is False
    assert r["metric_divergence"] is True and r["metric_lead_only"] is True
    assert evidence.inband_confirmation(_wrap(**r))[0] is False


# --- single_use ---

def test_single_use_confirms_coupon_replay():
    r = io.single_use(allowed_uses=1, observed_successes=3, control_enforced=True)
    assert r["confirmed"] is True and r["invariant_breached"] is True
    assert evidence.inband_confirmation(_wrap(**r))[0] is True


def test_single_use_rejects_when_no_limit_enforced():
    r = io.single_use(allowed_uses=1, observed_successes=3, control_enforced=False)
    assert r["confirmed"] is False
    assert evidence.inband_confirmation(_wrap(**r))[0] is False


# --- value_floor ---

def test_value_floor_confirms_negative_quantity_persisted():
    r = io.value_floor(observed_value=-5, floor=0, persisted=True)
    assert r["confirmed"] is True and r["below_floor"] is True
    assert evidence.inband_confirmation(_wrap(**r))[0] is True


def test_value_floor_rejects_not_persisted():
    r = io.value_floor(observed_value=-5, floor=0, persisted=False)
    assert r["confirmed"] is False
    assert evidence.inband_confirmation(_wrap(**r))[0] is False


# --- monotonic_state ---

def test_monotonic_state_confirms_regression():
    r = io.monotonic_state(from_rank=3, to_rank=1, control_enforced=True,
                           from_state="delivered", to_state="in_cart")
    assert r["confirmed"] is True and r["regressed"] is True
    assert evidence.inband_confirmation(_wrap(**r))[0] is True


def test_monotonic_state_rejects_forward_move():
    r = io.monotonic_state(from_rank=1, to_rank=3, control_enforced=True)
    assert r["confirmed"] is False
    assert evidence.inband_confirmation(_wrap(**r))[0] is False


def test_new_oracles_registered_in_gate():
    for name in ("auth_bypass", "behavioral_differential", "single_use", "value_floor", "monotonic_state"):
        assert name in evidence._INBAND_ORACLES


# --- cross_tenant_write_persist (shared-resource poisoning) ---

def test_cross_tenant_write_persist_confirms_shared_registry_poison():
    r = io.cross_tenant_write_persist(
        attacker_write={"status": 200, "body": "ok"},
        victim_read={"status": 200, "body": '{"provider":"anthropic","endpoint":"https://attacker.example/v1"}'},
        injected_marker="https://attacker.example/v1",
        control_read={"status": 200, "body": '{"provider":"anthropic","endpoint":"https://api.anthropic.com/v1"}'})
    assert r["confirmed"] is True and r["write_accepted"] is True and r["marker_persisted"] is True
    assert evidence.inband_confirmation(_wrap(**r))[0] is True


def test_cross_tenant_write_persist_rejects_blocked_write():
    r = io.cross_tenant_write_persist(
        attacker_write={"status": 403, "body": "forbidden"},
        victim_read={"status": 200, "body": "https://attacker.example/v1"},
        injected_marker="https://attacker.example/v1")
    assert r["confirmed"] is False
    assert evidence.inband_confirmation(_wrap(**r))[0] is False


def test_cross_tenant_write_persist_rejects_marker_in_control():
    r = io.cross_tenant_write_persist(
        attacker_write={"status": 200, "body": "ok"},
        victim_read={"status": 200, "body": "x"},
        injected_marker="x", control_read={"status": 200, "body": "x always here"})
    assert r["confirmed"] is False


def test_inband_confirmation_accepts_direct_reach_reflection():
    # Promotion gate (evidence.inband_confirmation) must accept the direct-reach SSRF receipt
    # (control not blocked, benign external) — the fix for the orkescloud azure_openai SSRF the
    # harness kept failing to promote. reached_backend + exclusive marker is the proof.
    from common import evidence
    base={"verification":evidence.INBAND_VERIFIER,"available":True,"confirmed":True,"oracle":"ssrf_differential"}
    ok,_ = evidence.inband_confirmation({**base,"model":"direct_reach_reflection",
        "control_blocked":False,"reached_backend":True,"marker":"INVALID_TOKEN"})
    assert ok is True
    # still rejects when backend not reached or no marker
    assert evidence.inband_confirmation({**base,"reached_backend":False,"marker":"x"})[0] is False
    assert evidence.inband_confirmation({**base,"reached_backend":True,"marker":""})[0] is False


def test_data_exfiltration_level4():
    recs = [{"email": f"u{i}@x.op", "pw": "h"} for i in range(10)]
    assert io.data_exfiltration(recs, 5, ["pii"])["confirmed"] is True
    # too few records -> not confirmed
    assert io.data_exfiltration(recs[:3], 5, ["pii"])["confirmed"] is False
    # enough rows but NO sensitive class (benign public list) -> not confirmed (FP guard)
    assert io.data_exfiltration(recs, 5, [])["confirmed"] is False
    # duplicates collapse to distinct
    assert io.data_exfiltration([{"a": 1}] * 10, 5, ["pii"])["record_count"] == 1


def test_destructive_persist_level4():
    pre = {"status": 200, "body": "order-42 total $9"}
    assert io.destructive_persist(pre, {"status": 404}, "order-42")["confirmed"] is True
    # resource never existed (pre absent) -> no-op delete cannot pass
    assert io.destructive_persist({"status": 404}, {"status": 404}, "order-42")["confirmed"] is False
    # still present after destroy -> not confirmed
    assert io.destructive_persist(pre, {"status": 200, "body": "order-42 total $9"}, "order-42")["confirmed"] is False


def test_availability_degradation_level4():
    degraded = [{"concurrency": 1, "p95_ms": 100, "error_rate": 0.0},
                {"concurrency": 8, "p95_ms": 900, "error_rate": 0.1}]
    assert io.availability_degradation(degraded)["confirmed"] is True
    flat = [{"concurrency": c, "p95_ms": 100 + c, "error_rate": 0.0} for c in (1, 2, 4, 8)]
    assert io.availability_degradation(flat)["confirmed"] is False


def test_level4_oracles_registered():
    for name in ("data_exfiltration", "destructive_persist", "availability_degradation"):
        assert name in io.LEVEL4_ORACLES
