"""Swarm standup-loop workers: seed -> standup -> (agents post facts) -> reconcile -> scribe."""
import types

import pytest


@pytest.fixture(autouse=True)
def _blackboard_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BLACKBOARD_DIR", str(tmp_path / "bb"))


def _task(wfid="wf-root", **inp):
    t = types.SimpleNamespace(input_data=inp)
    t.workflow_instance_id = wfid
    return t


def test_seed_then_standup_plans_a_wave():
    from swarm import tasks as sw
    cid = "campX"
    seed = sw.swarm_seed(_task(correlation_id=cid, catalog_objectives=[
        {"id": "INFRA-SSRF"}, {"id": "CONF-BOLA-CROSS-USER"}],
        identities={"userA": {}, "userB": {}}))
    assert len(seed["areas"]) == 2

    st = sw.swarm_standup(_task(correlation_id=cid, areas=seed["areas"], credits=seed["credits"],
                                width_ceiling=6, base_url="https://app.test", scope={"in_scope_hosts": ["app.test"]}))
    assert st["width"] == 6
    assert len(st["forkTasks"]) == 6
    # untested areas with no hypotheses -> scouts
    names = {ft["name"] for ft in st["forkTasks"]}
    assert "scout_agent" in names
    assert all(ref in st["forkTasksInputs"] for ref in [ft["taskReferenceName"] for ft in st["forkTasks"]])


def test_agents_post_facts_and_reconcile_synthesizes_and_decides():
    from swarm import tasks as sw
    cid = "campY"
    sw.swarm_seed(_task(correlation_id=cid, catalog_objectives=[{"id": "INFRA-SSRF"}],
                        identities={"userA": {}}))
    st = sw.swarm_standup(_task(correlation_id=cid, areas=[{"area": "INFRA-SSRF", "objective_id": "INFRA-SSRF"}],
                                credits={}, width_ceiling=2))
    watermark = st["fact_watermark"]

    # Two agents post complementary verified primitives -> should synthesize a combo chain.
    sw.swarm_post_fact(_task(wfid="wf-a", correlation_id=cid, kind="verified_primitive",
                             data={"area": "INFRA-SSRF", "kind": "internal_reach", "source": "SSRF task"}))
    sw.swarm_post_fact(_task(wfid="wf-b", correlation_id=cid, kind="verified_primitive",
                             data={"area": "INFRA-SSRF", "kind": "credential_held", "source": "leaked key"}))

    rec = sw.swarm_reconcile(_task(correlation_id=cid, credits={}, fact_watermark=watermark,
                                   round_idx=0, max_rounds=4, budget_left=1000,
                                   identities={"userA": {}}))
    objectives = {c["objective_id"] for c in rec["emergent_chains"]}
    assert "INFRA-RCE-INJECTION" in objectives          # combo synthesized
    assert rec["keep_going"] is True                     # fresh primitives -> continue
    assert rec["credits"]["INFRA-SSRF"] > 1.0            # yielding area boosted


def test_scribe_rollup_reports_confirmed():
    from swarm import tasks as sw
    cid = "campZ"
    sw.swarm_post_fact(_task(wfid="wf-a", correlation_id=cid, kind="finding",
                             data={"confirmed": True, "severity": "High", "title": "SSRF"}))
    roll = sw.scribe_rollup(_task(correlation_id=cid))
    assert roll["rollup"]["confirmed_count"] == 1
    assert len(roll["confirmed_findings"]) == 1
    assert roll["reproduction"][0]["workflow_id"] == "wf-a"


def test_post_fact_without_correlation_is_safe():
    from swarm import tasks as sw
    out = sw.swarm_post_fact(_task(correlation_id="", kind="observation", data={"x": 1}))
    assert out["posted"] == "" and "error" in out


def test_budget_fact_routes_to_lockfree_counter():
    from swarm import tasks as sw
    cid = "campBudget"
    sw.swarm_post_fact(_task(wfid="wf-a", correlation_id=cid, kind="budget",
                             data={"requests": 7, "bytes": 1200}))
    sw.swarm_post_fact(_task(wfid="wf-b", correlation_id=cid, kind="budget",
                             data={"requests": 3, "bytes": 300}))
    roll = sw.scribe_rollup(_task(correlation_id=cid))
    assert roll["rollup"]["spend"] == {"requests": 10, "bytes": 1500}


def test_refuter_excludes_unconfirmed_candidate_from_scribe():
    # The refuter excludes an UNCONFIRMED candidate it breaks (no oracle fired) — its real false-
    # positive filter. An oracle-confirmed finding would instead survive (covered in test_blackboard).
    from swarm import tasks as sw
    cid = "campRef"
    sw.swarm_post_fact(_task(wfid="wf-a", correlation_id=cid, kind="finding",
                             data={"confirmed": False, "severity": "High", "title": "Bogus"}))
    sw.swarm_post_fact(_task(wfid="wf-r", correlation_id=cid, kind="critique",
                             data={"survived": False, "candidate_title": "Bogus", "reason": "benign"}))
    roll = sw.scribe_rollup(_task(correlation_id=cid))
    assert roll["rollup"]["confirmed_count"] == 0
    assert len(roll["confirmed_findings"]) == 0


def test_oracle_confirmed_finding_survives_refuter_in_scribe():
    from swarm import tasks as sw
    cid = "campRef2"
    sw.swarm_post_fact(_task(wfid="wf-a", correlation_id=cid, kind="finding",
                             data={"confirmed": True, "severity": "High", "title": "Real SSRF"}))
    sw.swarm_post_fact(_task(wfid="wf-r", correlation_id=cid, kind="critique",
                             data={"survived": False, "candidate_title": "Real SSRF", "reason": "wrong call"}))
    roll = sw.scribe_rollup(_task(correlation_id=cid))
    assert roll["rollup"]["confirmed_count"] == 1  # oracle-confirmed survives the LLM refuter


def test_read_board_gives_peer_digest_excluding_self():
    from swarm import tasks as sw
    cid = "campCollab"
    sw.swarm_post_fact(_task(wfid="wf-a", correlation_id=cid, kind="verified_primitive",
                             data={"kind": "internal_reach", "source": "SSRF via /fetch"}))
    sw.swarm_post_fact(_task(wfid="wf-a", correlation_id=cid, kind="observation",
                             data={"note": "admin panel at /admin needs userB"}))
    sw.swarm_post_fact(_task(wfid="wf-b", correlation_id=cid, kind="claim", data={"area": "INFRA-SSRF"}))
    # wf-c reads the board: sees a + b's facts, not its own (none yet).
    dig = sw.swarm_read_board(_task(wfid="wf-c", correlation_id=cid, exclude_author="wf-c"))
    assert dig["available"] is True
    assert "internal_reach" in dig["digest"]          # peer's verified primitive
    assert "INFRA-SSRF" in dig["digest"]              # peer's claimed area
    assert dig["peer_count"] == 3


def test_read_board_excludes_own_facts():
    from swarm import tasks as sw
    cid = "campSelf"
    sw.swarm_post_fact(_task(wfid="wf-a", correlation_id=cid, kind="observation", data={"note": "mine"}))
    dig = sw.swarm_read_board(_task(wfid="wf-a", correlation_id=cid, exclude_author="wf-a"))
    assert dig["peer_count"] == 0 and dig["available"] is False


def test_read_board_no_correlation_is_noop():
    from swarm import tasks as sw
    dig = sw.swarm_read_board(_task(wfid="wf-x", correlation_id=""))
    assert dig["available"] is False and dig["digest"] == ""


def test_standup_dispatches_hypotheses_and_refuters_with_payloads():
    from swarm import tasks as sw
    cid = "campDepth"
    # Seed an area + an initial hypothesis + a confirmed finding needing review.
    sw.swarm_seed(_task(wfid="wf-seed", correlation_id=cid,
                        catalog_objectives=[{"id": "INFRA-SSRF"}],
                        identities={"userA": {}},
                        hypotheses=[{"objective_id": "INFRA-SSRF", "target": "/fetch",
                                     "category": "ssrf", "title": "SSRF via /fetch",
                                     "identities": ["userA"]}]))
    sw.swarm_post_fact(_task(wfid="wf-x", correlation_id=cid, kind="finding",
                             data={"confirmed": True, "title": "cross-tenant read", "severity": "High",
                                   "objective_id": "CONF-CROSS-TENANT-READ"}))
    st = sw.swarm_standup(_task(wfid="wf-lead", correlation_id=cid,
                                areas=[{"area": "INFRA-SSRF", "objective_id": "INFRA-SSRF", "untested": True}],
                                credits={}, width_ceiling=6,
                                base_url="https://app.test", app_model={}, identities={"userA": {}}))
    inputs = list(st["forkTasksInputs"].values())
    names = [ft["name"] for ft in st["forkTasks"]]
    # Exploiter got the hypothesis payload; refuter got the candidate; scout fills the rest.
    exploiters = [i for i in inputs if i.get("hypothesis")]
    refuters = [i for i in inputs if i.get("candidate")]
    assert exploiters and exploiters[0]["hypothesis"]["objective_id"] == "INFRA-SSRF"
    assert refuters and refuters[0]["candidate"]["title"] == "cross-tenant read"
    assert "refuter" in names
    assert st["roles"].get("exploiter", 0) >= 1 and st["roles"].get("refuter", 0) >= 1


def test_standup_retries_then_retires_a_hypothesis():
    from swarm import tasks as sw
    from common import blackboard as bb
    cid = "campTried"
    sw.swarm_seed(_task(wfid="wf-s", correlation_id=cid, catalog_objectives=[{"id": "X"}],
                        hypotheses=[{"objective_id": "X", "target": "/t", "category": "ssrf", "title": "H"}]))
    area = [{"area": "X", "objective_id": "X"}]
    # Dispatch it RETRY_MAX times across successive standups; it stays open (retrying) until exhausted.
    for i in range(bb.RETRY_MAX):
        assert len(bb.assemble_state(cid)["open_hypotheses"]) == 1, f"should still be retrying at round {i}"
        sw.swarm_standup(_task(wfid="wf-l", correlation_id=cid, areas=area, credits={},
                               width_ceiling=4, app_model={}, identities={}))
    # After RETRY_MAX dispatches it is retired.
    assert bb.assemble_state(cid)["open_hypotheses"] == []


def test_standup_feeds_prior_lesson_into_a_retry():
    from swarm import tasks as sw
    cid = "campRetryLesson"
    sw.swarm_seed(_task(wfid="wf-s", correlation_id=cid, catalog_objectives=[{"id": "X"}],
                        hypotheses=[{"objective_id": "X", "target": "/t", "category": "ssrf", "title": "H"}]))
    sw.swarm_standup(_task(wfid="wf-l", correlation_id=cid, areas=[{"area": "X", "objective_id": "X"}],
                           credits={}, width_ceiling=4, app_model={}, identities={}))
    # Record a failed attempt with a lesson, then the next standup must feed it into the retry.
    sw.swarm_post_fact(_task(wfid="wf-e", correlation_id=cid, kind="finding",
                             data={"confirmed": False, "signature": "ssrf|/t|",
                                   "lesson": "egress denylist blocked 127.0.0.1; try [::1]"}))
    st = sw.swarm_standup(_task(wfid="wf-l2", correlation_id=cid, areas=[{"area": "X", "objective_id": "X"}],
                                credits={}, width_ceiling=4, app_model={}, identities={}))
    retry_inp = [i for i in st["forkTasksInputs"].values() if i.get("hypothesis")]
    assert retry_inp and "RETRY" in retry_inp[0]["focus_directive"]
    assert "[::1]" in retry_inp[0]["focus_directive"]


def test_swarm_send_and_inbox_round_trip():
    from swarm import tasks as sw
    cid = "campMsg"
    sw.swarm_send(_task(wfid="wf-a", correlation_id=cid, to="wf-b", intent="ask",
                        body="which identity worked on /admin?", from_role="scout"))
    # wf-b (an exploiter) reads its inbox and sees the direct message + can see the board digest.
    box = sw.swarm_inbox(_task(wfid="wf-b", correlation_id=cid, me="wf-b", role="exploiter"))
    assert box["has_messages"] is True
    assert "which identity worked" in box["digest"]
    assert box["inbox"][0]["from"] == "wf-a"


def test_swarm_inbox_role_addressing():
    from swarm import tasks as sw
    cid = "campRole"
    sw.swarm_send(_task(wfid="wf-a", correlation_id=cid, to="role:chainer", intent="handoff",
                        body="SSRF primitive ready at /fetch, chain it"))
    chainer = sw.swarm_inbox(_task(wfid="wf-c", correlation_id=cid, me="wf-c", role="chainer"))
    scout = sw.swarm_inbox(_task(wfid="wf-s", correlation_id=cid, me="wf-s", role="scout"))
    assert "SSRF primitive ready" in chainer["digest"]
    assert scout["has_messages"] is False


def test_swarm_send_requires_recipient():
    from swarm import tasks as sw
    out = sw.swarm_send(_task(wfid="wf-a", correlation_id="c", to="", body="x"))
    assert out["sent"] == "" and "error" in out


def test_rehypothesize_prep_builds_from_observations():
    from swarm import tasks as sw
    cid = "campRehypo"
    # No observations yet -> have_observations False (round 1 relies on the seed batch).
    p0 = sw.swarm_rehypothesize_prep(_task(wfid="wf-l", correlation_id=cid, app_model={}, identities={}))
    assert p0["have_observations"] is False
    # After scouts post observations, prep says there IS learning to hypothesize from.
    sw.swarm_post_fact(_task(wfid="wf-a", correlation_id=cid, kind="observation",
                             data={"note": "admin API at /api/admin returns 200 for anon"}))
    p1 = sw.swarm_rehypothesize_prep(_task(wfid="wf-l", correlation_id=cid, app_model={}, identities={"userA": {}}))
    assert p1["have_observations"] is True and "admin API" in p1["prep"]


def test_absorb_posts_new_hypotheses_deduped():
    from swarm import tasks as sw
    from common import blackboard as bb
    cid = "campAbsorb"
    hyps = [{"objective_id": "INFRA-SSRF", "target": "/fetch", "category": "ssrf", "title": "SSRF"},
            {"objective_id": "INFRA-SSRF", "target": "/fetch", "category": "ssrf", "title": "SSRF dup"}]
    out = sw.swarm_absorb_hypotheses(_task(wfid="wf-l", correlation_id=cid, hypotheses=hyps))
    assert out["posted"] == 1  # same signature -> deduped to one
    # Re-absorbing the same one posts nothing new.
    out2 = sw.swarm_absorb_hypotheses(_task(wfid="wf-l", correlation_id=cid, hypotheses=hyps))
    assert out2["posted"] == 0
    assert len(bb.assemble_state(cid)["open_hypotheses"]) == 1


def test_standup_tags_hypothesis_with_confirming_oracle_and_directive():
    # Gap 1: every hypothesis-bearing assignment must carry the deterministic confirming oracle and a
    # focus_directive that names it, so the agent plants the ONE probe that can promote the finding.
    from swarm import tasks as sw
    cid = "campOracle"
    hyp = {"objective_id": "CONF-REGISTRY-POISON", "area": "POST /api/integrations/provider",
           "title": "shared built-in provider is overwritable cross-tenant", "category": "registry poison",
           "signature": "registry|post/api/integrations/provider|viren"}
    sw.bb.post_fact(cid, "lead", "hypothesis", hyp)
    st = sw.swarm_standup(_task(correlation_id=cid,
                                areas=[{"area": hyp["area"], "objective_id": hyp["objective_id"]}],
                                credits={}, width_ceiling=4))
    # find the assignment that carries this hypothesis
    carried = [i for i in st["forkTasksInputs"].values() if i.get("hypothesis")]
    assert carried, "at least one assignment should carry the hypothesis"
    tagged = [i for i in carried if i["hypothesis"].get("confirming_oracle")]
    assert tagged, "the hypothesis must be tagged with a confirming oracle"
    inp = tagged[0]
    assert inp["hypothesis"]["confirming_oracle"] == "cross_tenant_write_persist"
    assert inp["confirming_oracle"] == "cross_tenant_write_persist"
    assert "CONFIRMING ORACLE" in inp["focus_directive"]
    assert "cross_tenant_write_persist" in inp["focus_directive"]
    assert inp["hypothesis"].get("oracle_probe_fields")


def test_peer_digest_surfaces_live_attempt_lessons():
    # Gap 3: a teammate's latest probe lesson shows up in another agent's peer digest THIS round.
    from swarm import tasks as sw
    cid = "campLive"
    sw.bb.post_fact(cid, "agent-a", "attempt_lesson",
                    {"signature": "sqli|/login|anon", "area": "POST /login",
                     "lesson": "[union] /*!50000UNION*/ bypasses the WAF that blocked plain UNION"})
    pd = sw._peer_digest(cid, me="agent-b")
    assert pd["live_lessons"] == 1
    assert "LIVE tradecraft" in pd["digest"]
    assert "50000UNION" in pd["digest"]
    # the author's own lesson is NOT echoed back to themselves
    assert sw._peer_digest(cid, me="agent-a")["live_lessons"] == 0
