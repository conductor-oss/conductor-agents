"""Executable proof of the persistent-deepening invariants (design/DEEP_EXPLOITATION.md).

These tests are the machine-checked companion to the formal proof: they exercise the
no-premature-give-up guard (Theorem 2), the termination bound (Theorem 1), and the self-learning
lesson accumulation."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
from common import deepen  # noqa: E402


def test_ladder_selection_by_sink_class():
    assert deepen.ladder_for({"title": "SQL injection in /search", "category": "injection"})[0] == "sqli"
    assert deepen.ladder_for({"title": "ScriptEngine eval of tenant JS", "objective_id": "INFRA-RCE-INJECTION"})[0] == "js-sandbox-escape"
    assert deepen.ladder_for({"title": "SpEL in header", "category": "injection"})[0] == "injection"


def test_xss_and_traversal_ladders_route_and_walk():
    assert deepen.ladder_for({"title": "Sweep POST /feedback field comment for XSS"})[0] == "xss"
    assert deepen.ladder_for({"title": "Sweep GET /download field file for path traversal"})[0] == "traversal"
    assert deepen.ladder_for({"objective_id": "CLIENT-XSS-CSRF", "title": "stored xss in name"})[0] == "xss"
    assert deepen.ladder_for({"objective_id": "INFRA-PATH-TRAVERSAL", "title": "LFI in import"})[0] == "traversal"
    # the stored-xss rung (the high-severity prize) is on the xss ladder
    xss = deepen.init_state({"title": "xss in profile name"})
    assert xss["sink_class"] == "xss" and "stored-xss" in xss["ladder"]
    trav = deepen.init_state({"title": "path traversal in export filename"})
    assert trav["sink_class"] == "traversal" and "known-file" in trav["ladder"]


def test_next_family_walks_untried_then_under_tried_then_none():
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    ladder = st["ladder"]
    seen = []
    # breadth pass: one attempt per family (untried first)
    for _ in range(len(ladder)):
        nf = deepen.next_family(st)
        assert nf is not None
        seen.append(nf["family"])
        st = deepen.observe(st, nf["family"], lesson="blocked", result={"stdout": "200 ok"})
    assert seen == ladder, "breadth pass must visit every family once, in ladder order"
    # one pass is NOT exhaustion (depth-based): each family still has variants left
    assert deepen.exhausted(st) is False
    assert deepen.next_family(st)["tries"] == 1
    # drive every family to MAX_VARIANTS -> depth-exhausted, no family left
    for _ in range(len(ladder) * deepen.MAX_VARIANTS):
        nf = deepen.next_family(st)
        if nf is None:
            break
        st = deepen.observe(st, nf["family"], lesson="blocked", result={"stdout": "nope"})
    assert deepen.next_family(st) is None
    assert deepen.exhausted(st) is True


def test_gate_blocks_premature_giveup():
    """THE invariant: a 'not confirmed' conclusion after 1 attempt is REJECTED while families remain."""
    st = deepen.init_state({"title": "JS eval sandbox", "objective_id": "INFRA-RCE-INJECTION"})
    st = deepen.observe(st, "direct-eval", lesson="49 echoed but Java is undefined", result={"stdout": "49"})
    g = deepen.gate_conclude(st, proposed_confirmed=False)
    assert g["allow"] is False
    assert "reflection-breakout" in g["directive"] or "global-recon" in g["directive"]
    assert "49 echoed" in g["directive"]  # self-learning: prior lesson is fed forward


def test_gate_allows_on_confirmation_even_if_not_exhausted():
    """Early exit on success: an oracle hit ends the walk immediately, ladder not exhausted."""
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    receipt = {"verification": "oob_check/v1", "available": True, "hit": True,
               "hits": [{"token": "t", "path": "/c/t"}]}
    st = deepen.observe(st, "error-based", lesson="blind callback", result={}, oob_verification=receipt)
    assert st["confirmed"] is True
    assert deepen.exhausted(st) is False
    g = deepen.gate_conclude(st, proposed_confirmed=False)
    assert g["allow"] is True and g["reason"] == "confirmed"


def test_gate_allows_on_exhaustion_with_not_exploitable_verdict():
    """Exhaustion is DEPTH-based: a not-exploitable verdict is allowed only after every family has
    been tried MAX_VARIANTS times. One pass per family is NOT enough — the gate keeps it trying."""
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    # one breadth pass is not exhaustion any more — the gate still blocks
    for fam in list(st["ladder"]):
        st = deepen.observe(st, fam, lesson="filtered", result={"stdout": "blocked"})
    assert deepen.gate_conclude(st, proposed_confirmed=False)["allow"] is False
    # drive every family to MAX_VARIANTS -> now depth-exhausted -> give-up allowed
    for _ in range(deepen.MAX_VARIANTS):
        for fam in list(st["ladder"]):
            st = deepen.observe(st, fam, lesson="filtered", result={"stdout": "blocked"})
    g = deepen.gate_conclude(st, proposed_confirmed=False)
    assert g["allow"] is True
    assert g["reason"] == "ladder-exhausted"
    assert g.get("verdict") == "not-exploitable-after-exhaustive-escalation"


def test_detect_confirmation_requires_a_verifier_receipt():
    receipt = {"verification": "oob_check/v1", "available": True, "hit": True,
               "hits": [{"token": "x", "path": "/c/x"}]}
    assert deepen.detect_confirmation(oob_verification=receipt)[0] is True
    assert deepen.detect_confirmation({}, oob_hits=[{"token": "x"}])[0] is False
    assert deepen.detect_confirmation({"stdout": "uid=0(root) gid=0"})[0] is False
    assert deepen.detect_confirmation({"result": {"findings": [{"confirmed": True, "title": "rce"}]}})[0] is False
    assert deepen.detect_confirmation({"stdout": "HTTP 200 OK, nothing reflected"})[0] is False
    # Until a typed in-band verifier exists, stdout is never proof -- even a real-looking server error.
    assert deepen.detect_confirmation({"stdout": "SQLITE_ERROR: ... syntax error near \"'\""})[0] is False
    assert deepen.detect_confirmation({"stdout": "I sent q=' UNION SELECT 1,2,3-- and got 200"})[0] is False


def test_lessons_accumulate_and_feed_forward():
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    st = deepen.observe(st, "error-based", lesson="parameterized; no error leaks", result={"stdout": "200"})
    st = deepen.observe(st, "boolean-blind", lesson="response identical 1=1 vs 1=2", result={"stdout": "200"})
    dig = deepen.lessons_digest(st)
    assert "error-based" in dig and "boolean-blind" in dig
    assert "parameterized" in dig and "identical" in dig


def test_termination_bound_is_finite():
    """Theorem 1: the walk halts in at most |ladder| * MAX_VARIANTS observe() steps."""
    st = deepen.init_state({"title": "JS eval", "objective_id": "INFRA-RCE-INJECTION"})
    bound = len(st["ladder"]) * deepen.MAX_VARIANTS
    steps = 0
    while True:
        nf = deepen.next_family(st)
        if nf is None:
            break
        st = deepen.observe(st, nf["family"], lesson="x", result={"stdout": "no"})
        steps += 1
        assert steps <= bound, "must not exceed the proven bound"
    assert steps == bound


def test_ssrf_routes_to_dedicated_ladder_not_injection():
    """The proven gap: SSRF used to fall through to the code-injection ladder. It now walks the
    dedicated egress-bypass ladder so the internal-target corpus (incl. [::1]) is tried every run."""
    for hyp in (
        {"objective_id": "INFRA-SSRF", "title": "Exploit SSRF in HTTP task uri", "category": "ssrf"},
        {"title": "open-redirect in next param", "category": "open-redirect"},
        {"title": "reach internal 169.254.169.254 via http task"},
        {"title": "outbound fetch to metadata.google.internal"},
    ):
        assert deepen.ladder_for(hyp)[0] == "ssrf", hyp
    st = deepen.init_state({"objective_id": "INFRA-SSRF", "title": "SSRF in webhook", "category": "ssrf"})
    assert st["sink_class"] == "ssrf"
    # the loopback + ipv6 bypass rungs (where [::1] lives) are on the ladder
    assert "loopback-forms" in st["ladder"] and "ipv6-bypass" in st["ladder"]


def test_ssrf_focus_brief_surfaces_the_full_internal_target_corpus():
    """The corpus is DATA the agent must walk, surfaced every turn — so [::1] / [fd00:ec2::254] are
    never missed by ad-hoc recall."""
    st = deepen.init_state({"objective_id": "INFRA-SSRF", "title": "SSRF", "category": "ssrf"})
    brief = deepen.focus_brief(st)
    assert "[::1]" in brief and "[fd00:ec2::254]" in brief and "2130706433" in brief
    assert "blocked in this cluster" in brief   # teaches: a single egress 403 is NOT a dead end
    # a non-ssrf sink does not get the SSRF corpus dumped into its brief
    assert "[::1]" not in deepen.focus_brief(deepen.init_state({"title": "SQLi", "category": "injection"}))


def test_ssrf_stdout_is_a_lead_until_a_typed_verifier_exists():
    """Generated code can print an arbitrary backend-looking response. It is a useful lead, not a
    proof, until a typed differential/replay verifier is added."""
    block = {"stdout": "http://127.0.0.1:8080/actuator/env -> 403 "
                        "{\"message\":\"HTTP calls to this domain are blocked in this cluster\"}"}
    reach_401 = {"stdout": "http://[::1]:8080/actuator/env -> 401 "
                           "{\"error\":\"INVALID_TOKEN\",\"message\":\"Token cannot be null or empty\"}"}
    reach_docs = {"stdout": "http://[::1]:8080/api-docs -> 200 {\"openapi\":\"3.0\"}"}
    assert deepen.detect_confirmation(block, sink_class="ssrf")[0] is False
    assert deepen.detect_confirmation(reach_401, sink_class="ssrf")[0] is False
    assert deepen.detect_confirmation(reach_docs, sink_class="ssrf")[0] is False
    assert deepen.detect_confirmation(reach_401, sink_class="sqli")[0] is False
    st = deepen.init_state({"objective_id": "INFRA-SSRF", "title": "SSRF", "category": "ssrf"})
    st = deepen.observe(st, "canonical-internal", lesson="v4 blocked, trying [::1]", result=reach_401)
    assert st["confirmed"] is False


def test_stdout_oracles_are_not_confirmation_receipts():
    assert deepen.detect_confirmation({"stdout": "task output: 1787569"})[0] is False
    assert deepen.detect_confirmation({"stdout": "result is 49"})[0] is False
    assert deepen.detect_confirmation({"stdout": "[boot loader]\ntimeout=30"})[0] is False


def test_action_gate_only_allows_the_deterministic_next_family():
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    assert deepen.action_gate(st, "code", "boolean-blind")["allow"] is False
    allowed = deepen.action_gate(st, "code", "error-based")
    assert allowed["allow"] is True and allowed["expected_family"] == "error-based"
    st = deepen.observe(st, "error-based", lesson="no error", result={})
    assert deepen.action_gate(st, "code", "boolean-blind")["allow"] is True
    rejected = deepen.observe(st, "time-blind", lesson="should not count", result={})
    assert rejected["ledger"] == st["ledger"]
    assert rejected["last_observation"]["reason"] == "unexpected-family"


def test_model_claim_cannot_override_the_confirmation_gate():
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    gate = deepen.gate_conclude(st, proposed_confirmed=True)
    assert gate["allow"] is False and gate["confirmed"] is False


def test_init_state_carries_hypothesis_identifiers_and_attempt_op_tags():
    """Phase 2b: deepen state is self-describing (objective/cve/dependency) and attempt_op emits a
    deterministic family/CVE-tagged operation so technique_coverage + the cve_attempt gate see it
    without the agent calling sc.injection_attempt/sc.cve_attempt."""
    # a CVE hypothesis -> attempt_op emits a cve_attempt op carrying cve_id + family
    st = deepen.init_state({"objective_id": "INFRA-SUPPLY-CHAIN", "category": "cve",
                            "cve_id": "CVE-2026-44249", "dependency": "io.netty:netty-handler@4.1.133.Final",
                            "title": "Attempt CVE-2026-44249"})
    assert st["objective_id"] == "INFRA-SUPPLY-CHAIN" and st["cve_id"] == "CVE-2026-44249"
    op = deepen.attempt_op(st, st["ladder"][0], "blocked by allowlist", confirmed=False)
    assert op["type"] == "cve_attempt" and op["cve_id"] == "CVE-2026-44249"
    assert op["family"] == st["ladder"][0] and op["objective_id"] == "INFRA-SUPPLY-CHAIN"
    assert op["dependency"].startswith("io.netty")
    # a non-CVE injection hypothesis -> injection_attempt op (no cve_id)
    st2 = deepen.init_state({"objective_id": "INFRA-RCE-INJECTION", "category": "sqli", "title": "SQLi"})
    op2 = deepen.attempt_op(st2, "error-based", "no error reflected")
    assert op2["type"] == "injection_attempt" and "cve_id" not in op2 and op2["family"] == "error-based"


# --- Infra-vs-genuine attempt classification -----------------------------------------------
# code_exec tags its own pre-flight refusals with failure_class ("policy" | "infra"); a normal
# completed run (whatever its exit code) carries no failure_class at all. Neither a policy
# refusal nor an infra failure may count as a genuine ladder try -- otherwise sandbox/config
# noise falsely exhausts the ladder ("not-exploitable") or falsely satisfies an objective's
# completion gate (which trusts the mere presence of an attempt_op).

def test_classify_attempt():
    assert deepen.classify_attempt({"ok": True, "stdout": "200"}) == "genuine"
    assert deepen.classify_attempt({"ok": False}) == "genuine"                       # ran, exited non-zero
    assert deepen.classify_attempt({}) == "genuine"
    assert deepen.classify_attempt(None) == "genuine"
    assert deepen.classify_attempt({"failure_class": "policy"}) == "policy_refused"
    assert deepen.classify_attempt({"failure_class": "infra"}) == "infra_unavailable"
    assert deepen.classify_attempt({"failure_class": "something-new"}) == "genuine"  # fail open


def test_observe_infra_failure_does_not_increment_tries_or_confirm():
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    fam = st["ladder"][0]
    st = deepen.observe(st, fam, lesson="docker down",
                        result={"ok": False, "error": "docker not available on the worker host",
                                "failure_class": "infra"})
    assert st["ledger"].get(fam, {}).get("tries") is None  # never bumped
    assert st["ledger"][fam]["outcome"] == "infra_unavailable"
    assert st["ledger"][fam]["infra_failures"] == 1
    assert st["consecutive_infra_failures"] == 1
    assert st.get("confirmed") is not True
    assert st["last_observation"]["classification"] == "infra_unavailable"


def test_observe_policy_refusal_does_not_increment_tries_and_is_sticky():
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    fam = st["ladder"][0]
    st = deepen.observe(st, fam, lesson="capability too low",
                        result={"ok": False, "error": "refused: capability", "failure_class": "policy",
                                "refused_reason": "code_exec needs capability level 2 but the campaign is authorized to level 1"})
    assert st["ledger"].get(fam, {}).get("tries") is None
    assert st["ledger"][fam]["outcome"] == "policy_refused"
    assert "capability level 2" in st["ledger"][fam]["policy_reason"]
    assert st["policy_refused"] is True


def test_observe_genuine_attempt_still_increments_tries_and_resets_breaker():
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    fam = st["ladder"][0]
    st = deepen.observe(st, fam, lesson="infra blip",
                        result={"failure_class": "infra"})
    assert st["consecutive_infra_failures"] == 1
    st = deepen.observe(st, fam, lesson="ran this time, no error reflected",
                        result={"ok": False, "stdout": "200 generic"})
    assert st["ledger"][fam]["tries"] == 1            # the genuine attempt counted
    assert st["ledger"][fam]["outcome"] == "blocked"
    assert st["consecutive_infra_failures"] == 0      # reset by the genuine observation


def test_observe_lessons_list_excludes_infra_noise():
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    fam = st["ladder"][0]
    st = deepen.observe(st, fam, lesson="docker down", result={"failure_class": "infra"})
    assert st["lessons"] == []
    st = deepen.observe(st, fam, lesson="parameterized, no leak", result={"ok": False, "stdout": "200"})
    assert len(st["lessons"]) == 1 and st["lessons"][0]["lesson"] == "parameterized, no leak"


def test_gate_conclude_policy_refused_short_circuits_before_exhaustion():
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    fam = st["ladder"][0]
    st = deepen.observe(st, fam, lesson="no permission",
                        result={"failure_class": "policy", "error": "refused: capability"})
    gate = deepen.gate_conclude(st)
    assert gate["allow"] is True and gate["confirmed"] is False
    assert gate["reason"] == "policy-refused"
    assert gate["verdict"] == "not-attempted-policy-refused"


def test_gate_conclude_infra_circuit_breaker_trips_before_exhaustion():
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    fam = st["ladder"][0]
    for _ in range(deepen.INFRA_CIRCUIT_BREAKER):
        st = deepen.observe(st, fam, lesson="docker down", result={"failure_class": "infra"})
    # zero genuine tries anywhere -- exhaustion would ordinarily be far off
    assert all(deepen._tries(st, f) == 0 for f in st["ladder"])
    gate = deepen.gate_conclude(st)
    assert gate["allow"] is True and gate["confirmed"] is False
    assert gate["reason"] == "infra-unavailable"
    assert gate["verdict"] == "not-attempted-infra-unavailable"


def test_gate_conclude_infra_failures_below_breaker_still_demands_more_trying():
    st = deepen.init_state({"title": "SQLi", "category": "injection"})
    fam = st["ladder"][0]
    st = deepen.observe(st, fam, lesson="docker down", result={"failure_class": "infra"})
    gate = deepen.gate_conclude(st)
    assert gate["allow"] is False  # not yet at the breaker threshold, and ladder isn't exhausted


def test_infra_and_policy_never_satisfy_the_completion_gate_via_attempt_op():
    """The severe form of the bug: a single infra-classified attempt must never produce an
    attempt_op the caller would record into the operation ledger -- feature_exercise trusts the
    mere PRESENCE of that op type to mark INFRA-RCE-INJECTION/-SUPPLY-CHAIN/-SECRET-SURFACE
    'completed'. This mirrors the deepen_observe worker's accepted-and-genuine gating."""
    st = deepen.init_state({"objective_id": "INFRA-RCE-INJECTION", "category": "sqli", "title": "SQLi"})
    fam = st["ladder"][0]
    st = deepen.observe(st, fam, lesson="docker down", result={"failure_class": "infra"})
    last_obs = st["last_observation"]
    accepted = bool(last_obs.get("accepted"))
    genuine = last_obs.get("classification") == "genuine"
    op = deepen.attempt_op(st, fam, "docker down", confirmed=False) if (accepted and genuine) else {}
    assert op == {}, "an infra-failed attempt must not emit a completion-eligible operation"
