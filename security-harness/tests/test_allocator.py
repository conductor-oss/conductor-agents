"""Allocator: wave planning by credit, reconcile/synthesize, and the stop condition."""
from common import allocator as al


def test_plan_wave_scouts_spread_by_credit():
    areas = [{"area": "ssrf", "untested": True}, {"area": "authz", "untested": True}]
    credits = {"ssrf": 3.0, "authz": 1.0}
    plan = al.plan_wave(areas, [], [], credits, width_ceiling=8)
    counts = {}
    for a in plan["assignments"]:
        counts[a["area"]] = counts.get(a["area"], 0) + 1
    assert plan["width"] == 8
    assert all(a["role"] == "scout" for a in plan["assignments"])  # no hypotheses -> all scouts
    assert counts["ssrf"] > counts["authz"]


def test_plan_wave_respects_ceiling_and_budget():
    areas = [{"area": "a"}, {"area": "b"}, {"area": "c"}]
    plan = al.plan_wave(areas, [], [], {}, width_ceiling=100, budget_left=5)
    assert plan["width"] == 5  # budget caps below the ceiling


def test_plan_wave_dispatches_hypotheses_to_exploiters_and_chainers():
    hyps = [
        {"objective_id": "INFRA-SSRF", "target": "/fetch", "category": "ssrf", "title": "SSRF"},
        {"objective_id": "INFRA-RCE-INJECTION", "target": "a+b", "category": "chaining",
         "mandatory_kind": "emergent_chain", "title": "chain"},
    ]
    plan = al.plan_wave([{"area": "x", "untested": True}], hyps, [], {"x": 1}, width_ceiling=5)
    by_role = {a["role"]: a for a in plan["assignments"]}
    assert "exploiter" in by_role and by_role["exploiter"]["hypothesis"]["objective_id"] == "INFRA-SSRF"
    assert "chainer" in by_role and by_role["chainer"]["hypothesis"]["category"] == "chaining"
    assert "scout" in by_role  # remaining width fills with scouts


def test_plan_wave_dispatches_refuters_for_confirmed_candidates():
    cand = [{"title": "SSRF confirmed", "objective_id": "INFRA-SSRF", "confirmed": True}]
    plan = al.plan_wave([{"area": "x", "untested": True}], [], cand, {"x": 1}, width_ceiling=6)
    refs = [a for a in plan["assignments"] if a["role"] == "refuter"]
    assert len(refs) == 1 and refs[0]["candidate"]["title"] == "SSRF confirmed"


def test_plan_wave_refuters_do_not_crowd_out_the_wave():
    cand = [{"title": f"f{i}", "confirmed": True} for i in range(20)]
    plan = al.plan_wave([{"area": "x", "untested": True}], [], cand, {"x": 1}, width_ceiling=6)
    refs = sum(1 for a in plan["assignments"] if a["role"] == "refuter")
    assert refs <= 2  # ref_cap = ceiling//3


def test_reconcile_boosts_yielding_area_and_decays_dry():
    state = {"tried_signatures": [], "verified_primitives": []}
    facts = [
        {"kind": "verified_primitive", "data": {"area": "ssrf", "kind": "internal_reach"}},
        {"kind": "attempt", "data": {"area": "authz", "signature": "bola|/x|userA"}},
    ]
    out = al.reconcile(state, facts, credits={"ssrf": 1.0, "authz": 1.0})
    assert out["credit_updates"]["ssrf"] == 1.0 + al.BOOST    # yielded -> boosted
    assert out["credit_updates"]["authz"] == 1.0 * al.DECAY    # dry -> decayed
    assert "bola|/x|userA" in out["tried_signatures"]


def test_reconcile_synthesizes_emergent_chain_from_two_primitives():
    state = {"tried_signatures": [],
             "verified_primitives": [{"kind": "internal_reach", "source": "SSRF via HTTP task"}]}
    facts = [{"kind": "verified_primitive", "data": {"kind": "credential_held", "source": "leaked key"}}]
    out = al.reconcile(state, facts, identity_labels=["userA"])
    objectives = {c["objective_id"] for c in out["emergent_chains"]}
    # internal_reach + credential_held compose into a deeper RCE chain no single agent reached.
    assert "INFRA-RCE-INJECTION" in objectives


def test_should_continue_stops_when_dry():
    # No open hypotheses, no review candidates, no untested areas, no progress -> dry stop.
    v = al.should_continue({"coverage": {}, "open_hypotheses": [], "review_candidates": []},
                           round_idx=2, max_rounds=8, budget_left=100, made_progress=False)
    assert v["continue"] is False and "dry" in v["reason"]


def test_should_continue_stops_at_round_cap_and_budget():
    assert al.should_continue({"open_hypotheses": [{"x": 1}]}, round_idx=8, max_rounds=8,
                              budget_left=10, made_progress=True)["continue"] is False
    assert al.should_continue({"open_hypotheses": [{"x": 1}]}, round_idx=1, max_rounds=8,
                              budget_left=0, made_progress=True)["continue"] is False


def test_should_continue_runs_while_work_remains():
    # Open hypotheses (incl. retries) keep the loop going -- this is what prevents zero-shot.
    assert al.should_continue({"open_hypotheses": [{"x": 1}]}, round_idx=1, max_rounds=8,
                              budget_left=10, made_progress=False)["continue"] is True
    # Review candidates keep it going too.
    assert al.should_continue({"review_candidates": [{"t": 1}]}, round_idx=1, max_rounds=8,
                              budget_left=10, made_progress=False)["continue"] is True
    # Progress (a new primitive) buys one more round even with an empty queue (chains may form).
    assert al.should_continue({"open_hypotheses": [], "review_candidates": [], "coverage": {}},
                              round_idx=1, max_rounds=8, budget_left=10,
                              made_progress=True)["continue"] is True


def test_work_remaining_counts_real_work():
    wr = al.work_remaining({"open_hypotheses": [{}, {}], "review_candidates": [{}],
                            "coverage": {"a": {"untested": True}, "b": {"untested": False}}})
    assert wr["open_hypotheses"] == 2 and wr["review_candidates"] == 1
    assert wr["untested_areas"] == 1 and wr["total"] == 4


def test_plan_wave_diversifies_the_ensemble():
    # Scouts in one wave must differ across model/persona/temperature (not clones).
    areas = [{"area": "ssrf", "untested": True}]
    div = {"models": ["opus", "sonnet"], "personas": ["p1", "p2", "p3"]}
    plan = al.plan_wave(areas, [], [], {"ssrf": 6.0}, width_ceiling=6, diversity=div)
    combos = {(a.get("model"), a.get("persona"), a.get("temperature")) for a in plan["assignments"]}
    assert len(combos) > 1                      # genuinely diverse
    assert {a["model"] for a in plan["assignments"]} == {"opus", "sonnet"}


def test_scout_floor_reserves_breadth_in_hypothesis_rich_wave():
    # A hypothesis-rich wave must NOT fill 100% width with exploiters — reserve a scout floor so
    # breadth/understanding (and docs lookup via search_docs) always runs. (Regression: orkescloud
    # run allocated 10 exploiters / 0 scouts, so no agent ever used the docs.)
    from common import allocator as al
    hyps = [{"title": f"h{i}", "objective_id": f"O{i}"} for i in range(20)]
    areas = [{"area": f"A{i}", "objective_id": f"A{i}", "untested": True} for i in range(5)]
    plan = al.plan_wave(areas, open_hypotheses=hyps, width_ceiling=10)
    roles = {}
    for a in plan["assignments"]:
        roles[a["role"]] = roles.get(a["role"], 0) + 1
    assert plan["width"] == 10
    assert roles.get("scout", 0) >= 1, f"expected a scout floor, got {roles}"
    assert roles.get("exploiter", 0) >= 1, f"exploiters must still dominate, got {roles}"


def test_scout_floor_not_applied_when_no_untested_areas():
    from common import allocator as al
    hyps = [{"title": f"h{i}", "objective_id": f"O{i}"} for i in range(12)]
    plan = al.plan_wave([], open_hypotheses=hyps, width_ceiling=8)
    roles = {a["role"] for a in plan["assignments"]}
    assert "scout" not in roles  # nothing to scout -> all depth
    assert plan["width"] == 8


def test_reconcile_marks_worked_areas_tested_so_backlog_shrinks():
    # Keystone fix (run 05b95dd9: exploiters worked 16 areas but only 2 were ever marked tested, so
    # the loop declared "dry" with 89% of the surface unhunted). Any seeded area that got an
    # attempt/lesson/observation this round must drop out of the untested backlog.
    import importlib
    al = importlib.import_module("common.allocator")
    state = {"coverage": {
        "A1": {"area": "A1", "status": "untested", "untested": True},
        "A2": {"area": "A2", "status": "untested", "untested": True},
        "A3": {"area": "A3", "status": "untested", "untested": True},
    }}
    facts = [
        {"kind": "attempt_lesson", "data": {"objective_id": "A1", "lesson": "x"}},
        {"kind": "observation", "data": {"objective_id": "A2"}},
        {"kind": "finding", "data": {"objective_id": "A3", "confirmed": True}},   # yielded → exploited
    ]
    out = al.reconcile(state, facts, credits={})
    cu = out["coverage_updates"]
    assert cu["A1"]["untested"] is False and cu["A1"]["status"] == "scouted"
    assert cu["A2"]["untested"] is False
    assert cu["A3"]["status"] == "exploited"


def test_reconcile_seed_coverage_facts_do_not_self_certify():
    # A seed coverage fact (status=untested) must NOT count as "work" — only real attempts/lessons
    # do. Otherwise every area marks itself tested the instant it is seeded.
    import importlib
    al = importlib.import_module("common.allocator")
    state = {"coverage": {"A1": {"area": "A1", "status": "untested", "untested": True}}}
    facts = [{"kind": "coverage", "data": {"area": "A1", "status": "untested", "untested": True}}]
    out = al.reconcile(state, facts, credits={})
    assert out["coverage_updates"].get("A1", {}).get("untested") is not False


def test_scout_floor_scales_with_untested_backlog():
    import importlib
    al = importlib.import_module("common.allocator")
    many_untested = [{"area": f"A{i}", "objective_id": f"A{i}", "untested": True} for i in range(30)]
    hyps = [{"title": f"h{i}", "objective_id": f"O{i}"} for i in range(30)]
    w = al.plan_wave(many_untested, open_hypotheses=hyps, width_ceiling=24)
    scouts = sum(1 for a in w["assignments"] if a["role"] == "scout")
    assert scouts >= 24 // 2 - 1, f"breadth starved: only {scouts} scouts for 30 untested areas"
    # with few untested areas, depth dominates again
    few = [{"area": "A1", "objective_id": "A1", "untested": True}]
    w2 = al.plan_wave(few, open_hypotheses=hyps, width_ceiling=24)
    assert sum(1 for a in w2["assignments"] if a["role"] == "scout") <= 2
