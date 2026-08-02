"""Tests for the tail-feature graph: bucket classification, tail-risk scoring, the budget-bounded
scheduling reservation, and tail-coverage accounting (PLAN_V3 Phases 1-3)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
from common import feature_graph as fg  # noqa: E402


def _feat(fid, path, source="surface", inputs=None, sink_hints=None, tail=False, **kw):
    return {"id": fid, "method": kw.get("method", "GET"), "path": path, "source": source,
            "inputs": inputs or [], "sink_hints": sink_hints or [], "tail": tail, **kw}


# ───────────────────────── Phase 1: classification + tail-risk ─────────────────────────
def test_classify_buckets_by_cue():
    assert fg.LEGACY in fg.classify_one(_feat("a", "/api/legacy/orders"))[0]
    assert fg.ADMIN_DEBUG in fg.classify_one(_feat("b", "/admin/users"))[0]
    assert fg.IMPORT_EXPORT in fg.classify_one(_feat("c", "/api/export/report"))[0]
    assert fg.INTEGRATION in fg.classify_one(_feat("d", "/api/webhook/incoming"))[0]
    # a plain, observed, unremarkable endpoint is core (no tail bucket)
    assert fg.classify_one(_feat("e", "/api/profile"))[0] == [fg.CORE]


def test_source_only_is_hidden_and_scores_higher():
    live = _feat("live", "/api/orders", source="surface")
    src_only = _feat("src", "/api/orders", source="model,docs")
    b_live, r_live = fg.classify_one(live)
    b_src, r_src = fg.classify_one(src_only)
    assert fg.HIDDEN in b_src and fg.HIDDEN not in b_live
    assert r_src > r_live                     # source-only is up-weighted, popularity aside


def test_low_traffic_tag_and_definition_input_risk():
    tailf = _feat("t", "/api/misc", tail=True)
    assert fg.LOW_TRAFFIC in fg.classify_one(tailf)[0]
    deff = _feat("wf", "INLINE", inputs=[{"name": "expression", "location": "definition"}])
    _, risk = fg.classify_one(deff)
    assert risk >= fg._risk_weights()["definition_input"]


def test_tail_risk_independent_of_popularity():
    """A neglected admin/legacy route outranks a popular core route regardless of prio/rank."""
    popular_core = _feat("core", "/api/dashboard", source="surface", tail=False)
    popular_core["prio"] = 99
    neglected = _feat("neg", "/admin/legacy/debug", source="model", tail=True)
    neglected["prio"] = 0
    assert fg.classify_one(neglected)[1] > fg.classify_one(popular_core)[1]


def test_build_graph_alternate_interface_and_versioning():
    inv = [
        _feat("v1", "/api/v1/workflows", source="surface"),
        _feat("v2", "/api/v2/workflows", source="surface"),
        _feat("gql", "/graphql", source="surface", method="POST"),
        _feat("plain", "/api/profile", source="surface"),
    ]
    g = fg.build_graph(inv)
    byid = {f["id"]: f for f in g["features"]}
    assert fg.ALTERNATE_INTERFACE in byid["v1"]["buckets"]
    assert fg.ALTERNATE_INTERFACE in byid["v2"]["buckets"]
    assert fg.LEGACY in byid["v1"]["buckets"]          # older version = legacy
    assert fg.LEGACY not in byid["v2"]["buckets"]
    assert fg.ALTERNATE_INTERFACE in byid["gql"]["buckets"]
    assert byid["plain"]["buckets"] == [fg.CORE]
    assert g["total"] == 4 and g["tail_total"] == 3    # plain is the only core feature


# ───────────────────────── Phase 2: budget-bounded reservation ─────────────────────────
def _graph_of(*feats):
    return fg.build_graph(list(feats))


def _coverage(untested_ids, other=None):
    per = [{"id": i, "status": "untested"} for i in untested_ids]
    per += [{"id": i, "status": s} for i, s in (other or {}).items()]
    return {"per_feature": per}


def test_reserve_fractions_min_and_budget_bound():
    feats = [_feat(f"leg{i}", f"/api/legacy/x{i}") for i in range(6)]
    g = _graph_of(*feats)
    cov = _coverage([f["id"] for f in feats])
    r = fg.reserve(g, cov, explore_slots=10, exploit_slots=8)
    assert r["explore_reserve"] == 4          # max(2, ceil(0.35*10))
    assert r["exploit_reserve"] == 2          # max(1, ceil(0.25*8))
    assert len(r["explore"]) == 4
    # budget bound: 1 slot cannot become 2 even though the min is 2 (C3)
    r1 = fg.reserve(g, cov, explore_slots=1, exploit_slots=0)
    assert r1["explore_reserve"] == 1 and r1["exploit_reserve"] == 0


def test_reserve_bucket_diversity_before_repeat():
    """One from each non-empty untested bucket before any bucket is drawn from twice."""
    feats = [
        _feat("adm1", "/admin/a"), _feat("adm2", "/admin/b"), _feat("adm3", "/admin/c"),
        _feat("imp1", "/api/import/a"), _feat("int1", "/api/webhook/a"),
    ]
    g = _graph_of(*feats)
    cov = _coverage([f["id"] for f in feats])
    r = fg.reserve(g, cov, explore_slots=6, exploit_slots=0)  # reserve = max(2, ceil(2.1))=3
    picked = set(r["explore"])
    byid = {f["id"]: f for f in g["features"]}
    buckets_hit = {b for pid in picked for b in byid[pid]["buckets"] if b in fg.TAIL_BUCKETS}
    # all three distinct buckets are represented before admin is taken a 3rd time
    assert {fg.ADMIN_DEBUG, fg.IMPORT_EXPORT, fg.INTEGRATION} <= buckets_hit


def test_reserve_exploit_only_source_backed_and_excludes_covered():
    eligible = _feat("sink", "/api/legacy/search", sink_hints=["SQL built from input"])
    bare = _feat("bare", "/api/legacy/list")
    g = _graph_of(eligible, bare)
    cov = _coverage(["sink", "bare"])
    r = fg.reserve(g, cov, explore_slots=4, exploit_slots=4)
    assert r["exploit"] == ["sink"]           # only the sink-backed feature is exploit-eligible
    # covered_ids removes a feature from the pool entirely
    r2 = fg.reserve(g, cov, explore_slots=4, exploit_slots=4, covered_ids={"sink"})
    assert "sink" not in r2["explore"] and r2["exploit"] == []


def test_reserve_ignores_non_tail_and_already_tested():
    tail_untested = _feat("adm", "/admin/x")
    core_untested = _feat("core", "/api/profile")
    tail_tested = _feat("adm2", "/admin/y")
    g = _graph_of(tail_untested, core_untested, tail_tested)
    cov = _coverage(["adm", "core"], other={"adm2": "deep-exploited"})
    r = fg.reserve(g, cov, explore_slots=8, exploit_slots=0)
    assert r["explore"] == ["adm"]            # core excluded, already-tested tail excluded


# ───────────────────────── Phase 3: tail-coverage accounting ─────────────────────────
def test_tail_coverage_lifecycle_and_omitted():
    feats = [
        _feat("u", "/admin/untested"),
        _feat("b", "/admin/blocked"),
        _feat("c", "/api/export/clean"),
        _feat("s", "/api/webhook/signal"),
        _feat("v", "/api/legacy/vuln", sink_hints=["x"]),
    ]
    g = _graph_of(*feats)
    cov = {"per_feature": [
        {"id": "u", "status": "untested"},
        {"id": "b", "status": "blocked"},
        {"id": "c", "status": "triaged-clean"},
        {"id": "s", "status": "signal"},
        {"id": "v", "status": "deep-exploited"},
    ]}
    confirmed = [{"feature_id": "v", "title": "SQLi"}]
    tc = fg.tail_coverage(g, cov, confirmed=confirmed)
    assert tc["totals"]["verified_vulnerable"] == 1
    assert tc["totals"]["untested"] == 1 and tc["totals"]["blocked"] == 1
    assert tc["totals"]["baseline_exercised"] == 1      # triaged-clean
    assert tc["totals"]["adversarially_probed"] == 1    # signal
    omitted_ids = {o["id"] for o in tc["omitted"]}
    assert omitted_ids == {"u", "b"}                    # untested + blocked are the residual
    assert fg.residual_sentence(tc)                     # non-empty when something omitted


def test_residual_sentence_empty_when_nothing_omitted():
    feats = [_feat("s", "/api/webhook/x")]
    g = _graph_of(*feats)
    cov = {"per_feature": [{"id": "s", "status": "deep-exploited"}]}
    tc = fg.tail_coverage(g, cov)
    assert tc["omitted_count"] == 0
    assert fg.residual_sentence(tc) == ""


# ───────────────────────── C2: tunables are config-lineage data, not literals ─────────────────────────
def test_tunables_overlaid_from_tradecraft_data(tmp_path):
    """PLAN_V3 C2: the reservation fractions and tail-risk weights load through `tradecraft` data so
    the benchmark/hill-climber can move them under ratify -- they are not hardcoded literals."""
    import yaml
    from common import tradecraft
    f = tmp_path / "tc.yaml"
    f.write_text(yaml.safe_dump({
        "tail_reservation": {"explore_fraction": 0.5, "explore_min": 3,
                             "exploit_fraction": 0.25, "exploit_min": 1},
        "tail_risk_weights": {"legacy": 99.0},
    }))
    tradecraft.load.cache_clear()
    try:
        os.environ["SC_TRADECRAFT"] = str(f)
        tradecraft.load.cache_clear()
        assert fg.reservation_params()["explore_fraction"] == 0.5
        assert fg.reservation_params()["explore_min"] == 3
        _, risk = fg.classify_one(_feat("l", "/api/legacy/x"))
        assert risk >= 99.0                    # overridden legacy weight took effect
    finally:
        os.environ.pop("SC_TRADECRAFT", None)
        tradecraft.load.cache_clear()
