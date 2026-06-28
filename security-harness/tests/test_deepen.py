"""Executable proof of the persistent-deepening invariants (docs/DEEP_EXPLOITATION.md).

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
    st = deepen.observe(st, "time-blind", lesson="5s delay", result={}, oob_hits=[{"token": "t"}])
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


def test_detect_confirmation_sources():
    assert deepen.detect_confirmation({}, oob_hits=[{"token": "x"}])[0] is True
    assert deepen.detect_confirmation({"stdout": "uid=0(root) gid=0"})[0] is True
    assert deepen.detect_confirmation({"result": {"findings": [{"confirmed": True, "title": "rce"}]}})[0] is True
    assert deepen.detect_confirmation({"stdout": "HTTP 200 OK, nothing reflected"})[0] is False
    # server-emitted SQL error confirms; the attacker's OWN payload echoed back must NOT
    assert deepen.detect_confirmation({"stdout": "SQLITE_ERROR: ... syntax error near \"'\""})[0] is True
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


def test_ssrf_internal_reach_oracle_distinguishes_block_from_reach():
    """The other half of the false-negative: a 403 'blocked in this cluster' is BLOCKED, but a
    non-403 backend response from an internal target (health body, internal OpenAPI, or a backend
    INVALID_TOKEN proving traversal) is a CONFIRMED internal reach. Gated on the ssrf sink so an
    unrelated auth-test 401 cannot false-confirm."""
    block = {"stdout": "http://127.0.0.1:8080/actuator/env -> 403 "
                        "{\"message\":\"HTTP calls to this domain are blocked in this cluster\"}"}
    reach_401 = {"stdout": "http://[::1]:8080/actuator/env -> 401 "
                           "{\"error\":\"INVALID_TOKEN\",\"message\":\"Token cannot be null or empty\"}"}
    reach_docs = {"stdout": "http://[::1]:8080/api-docs -> 200 {\"openapi\":\"3.0\"}"}
    assert deepen.detect_confirmation(block, sink_class="ssrf")[0] is False        # BLOCKED, not confirmed
    assert deepen.detect_confirmation(reach_401, sink_class="ssrf")[0] is True     # backend traversal proof
    assert deepen.detect_confirmation(reach_docs, sink_class="ssrf")[0] is True    # internal OpenAPI reached
    # the SAME backend-401 in a non-SSRF sink must NOT confirm (guard against false positives)
    assert deepen.detect_confirmation(reach_401, sink_class="sqli")[0] is False
    # observe() threads the sink_class through, so an ssrf reach flips state.confirmed
    st = deepen.init_state({"objective_id": "INFRA-SSRF", "title": "SSRF", "category": "ssrf"})
    st = deepen.observe(st, "ipv6-bypass", lesson="v4 blocked, trying [::1]", result=reach_401)
    assert st["confirmed"] is True and "internal SSRF reach" in st["confirm_evidence"]


def test_ssti_distinctive_product_and_winfile_oracles():
    """Phase 2 per-class oracles: the distinctive SSTI product confirms (7*7=49 does not), and
    Windows server-file content confirms traversal."""
    assert deepen.detect_confirmation({"stdout": "task output: 1787569"})[0] is True
    assert deepen.detect_confirmation({"stdout": "result is 49"})[0] is False
    assert deepen.detect_confirmation({"stdout": "[boot loader]\ntimeout=30"})[0] is True


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
