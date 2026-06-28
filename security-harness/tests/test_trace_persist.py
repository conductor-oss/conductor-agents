"""Persisted trace corpus (P3-5): findings -> verdict-with-reasons records under the per-deployment
state dir, so the §19 hc_analyze loop mines REAL run traces instead of a mirrored corpus."""
from common import trace, memory


def test_from_findings_builds_verdict_records():
    recs = trace.from_findings(
        "run1", "2026-01-01T00:00:00Z",
        confirmed=[{"objective_id": "INFRA-RCE-INJECTION", "class": "infra",
                    "evidence": "oob canary hit from target", "content_hash": "abc123"}],
        rejected=[{"objective_id": "CONF-CROSS-TENANT-READ", "class": "tenancy", "title": "no contrast"}],
        blind=[{"objective_id": "INFRA-SSRF", "class": "infra"}])
    outcomes = {r["objective_id"]: r["outcome"] for r in recs}
    assert outcomes == {"INFRA-RCE-INJECTION": "confirmed",
                        "CONF-CROSS-TENANT-READ": "rejected",
                        "INFRA-SSRF": "inconclusive"}
    conf = next(r for r in recs if r["objective_id"] == "INFRA-RCE-INJECTION")
    assert conf["finding_sig"] == "abc123" and conf["evidence_bar"] == "infra"
    assert "oob canary hit" in conf["reason"]


def test_traces_path_round_trip_and_clustering(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    p = memory.traces_path("dev.example.com-abc")
    assert p.endswith("traces.jsonl") and str(tmp_path) in p
    # persist + load round-trips (the corpus hc_analyze reads)
    for r in trace.from_findings("r1", "t",
                                 confirmed=[{"objective_id": "INFRA-RCE-INJECTION", "class": "infra"}]):
        trace.persist(p, r)
    loaded = trace.load(p)
    assert loaded and loaded[0]["objective_id"] == "INFRA-RCE-INJECTION"
    # H4: a signature recurring across runs is mined as a recurring cluster
    corpus = [trace.record(run_id="r1", objective_id="INFRA-SUPPLY-CHAIN", outcome="inconclusive", as_of="t"),
              trace.record(run_id="r2", objective_id="INFRA-SUPPLY-CHAIN", outcome="inconclusive", as_of="t")]
    rec = trace.recurring(corpus, min_count=2)
    assert rec and rec[0]["objective_id"] == "INFRA-SUPPLY-CHAIN" and rec[0]["count"] == 2
