"""Structured trace corpus (§19.2 / P3-5): verdict-with-reasons, clustered, corroborated (H4)."""
from common import trace

T0 = "2026-06-21T00:00:00Z"


def test_record_captures_the_verdict_reason():
    r = trace.record(run_id="r1", objective_id="CONF-CROSS-TENANT-READ", outcome="rejected",
                     as_of=T0, evidence_bar="cross-identity contrast",
                     reason="only the caller's own data was returned")
    assert r["outcome"] == "rejected" and r["evidence_bar"] == "cross-identity contrast"
    assert "own data" in r["reason"]
    # invalid outcomes degrade safely
    assert trace.record(run_id="r", objective_id="X", outcome="bogus", as_of=T0)["outcome"] == "inconclusive"


def test_failures_and_persistence_round_trip(tmp_path):
    store = []
    trace.append(store, trace.record(run_id="r1", objective_id="A", outcome="confirmed", as_of=T0))
    trace.append(store, trace.record(run_id="r1", objective_id="B", outcome="rejected", as_of=T0))
    assert [r["objective_id"] for r in trace.failures(store)] == ["B"]
    p = str(tmp_path / "fp" / "trace.jsonl")
    for r in store:
        trace.persist(p, r)
    assert len(trace.load(p)) == 2


def test_clustering_surfaces_recurring_signatures_only():
    store = []
    # objective A rejected for the same bar 3× across runs -> a recurring signature (H4)
    for i in range(3):
        trace.append(store, trace.record(run_id=f"r{i}", objective_id="AUTHZ-FUNCTION-LEVEL",
                                          outcome="rejected", as_of=T0, evidence_bar="action took effect",
                                          reason=f"403 on attempt {i}"))
    # objective B rejected once -> noise, must not be acted on
    trace.append(store, trace.record(run_id="r9", objective_id="B", outcome="rejected", as_of=T0,
                                     evidence_bar="x", reason="one-off"))
    clusters = trace.cluster(store)
    assert clusters[0]["objective_id"] == "AUTHZ-FUNCTION-LEVEL" and clusters[0]["count"] == 3
    assert clusters[0]["examples"][:1] == ["403 on attempt 0"]
    recurring = trace.recurring(store, min_count=2)
    assert {c["objective_id"] for c in recurring} == {"AUTHZ-FUNCTION-LEVEL"}   # B (count 1) excluded


def test_enriched_builders_capture_attempt_level_signal():
    # technique_coverage: many families tried, none confirmed -> per-family 'exhausted' deepen traces
    tc = {"INFRA-RCE-INJECTION": {"tried_families": ["syntactic", "alternate-engine", "reflection-breakout"], "n_tried": 3}}
    recs = trace.from_technique_coverage("r1", T0, tc)
    assert recs and all(r["signal_kind"] == "deepen" and r["outcome"] == "exhausted" for r in recs)
    assert {r["family"] for r in recs} == {"syntactic", "alternate-engine", "reflection-breakout"}
    # low breadth -> a technique_coverage breadth-gap trace
    low = trace.from_technique_coverage("r1", T0, {"INFRA-SSRF": {"tried_families": ["syntactic"], "n_tried": 1}})
    assert low[0]["signal_kind"] == "technique_coverage" and low[0]["n_tried"] == 1
    # a confirmed objective produces no failure trace
    assert trace.from_technique_coverage("r1", T0, tc, confirmed=[{"objective_id": "INFRA-RCE-INJECTION"}]) == []


def test_from_deepen_states_carries_lesson_in_reason_not_key():
    st = [{"objective_id": "INFRA-RCE-INJECTION", "sink_class": "js-sandbox-escape", "exhausted": True,
           "ledger": {"reflection-breakout": {"tries": 2, "lesson": "Java is undefined in the engine"}}}]
    recs = trace.from_deepen_states("r1", T0, st)
    assert recs[0]["family"] == "reflection-breakout" and recs[0]["sink_class"] == "js-sandbox-escape"
    assert recs[0]["outcome"] == "exhausted" and "Java is undefined" in recs[0]["reason"]
    # the lesson (free text) must NOT appear in the clustering signature (H7)
    assert "Java is undefined" not in trace._sig(recs[0])
    assert "reflection-breakout" in trace._sig(recs[0]) and "js-sandbox-escape" in trace._sig(recs[0])


def test_from_triage_flags_unconfirmed_class():
    sigs = [{"feature_id": "GET:/search", "class": "sqli"}, {"feature_id": "GET:/search", "class": "sqli"}]
    recs = trace.from_triage("r1", T0, sigs)
    assert len(recs) == 1 and recs[0]["signal_kind"] == "triage" and recs[0]["triage_class"] == "sqli"
    assert recs[0]["objective_id"] == "INFRA-RCE-INJECTION"
    # if a sqli finding was confirmed, no gap trace
    assert trace.from_triage("r1", T0, sigs, confirmed=[{"category": "sqli"}]) == []


def test_new_outcomes_and_clustering_of_enriched_traces():
    assert "exhausted" in trace.OUTCOMES and "blocked" in trace.OUTCOMES
    recs = [trace.record(run_id="r", as_of=T0, objective_id="INFRA-RCE-INJECTION", outcome="exhausted",
                         signal_kind="deepen", family="reflection-breakout", sink_class="js-sandbox-escape")
            for _ in range(2)]
    rec = trace.recurring(recs, min_count=2)
    assert rec and rec[0]["family"] == "reflection-breakout" and rec[0]["count"] == 2
