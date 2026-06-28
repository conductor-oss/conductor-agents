"""Tests for the feature inventory + reflection classifier (feature-complete sweep)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
from common import features  # noqa: E402


def test_build_inventory_merges_all_sources():
    app_model = {
        "features": [{"name": "search", "endpoints": ["GET /api/search", "POST /api/items"]}],
        "trust_boundaries": ["SQL queries built from input on /api/search (formatted-sql)"],
    }
    surface = {
        "endpoints": [{"method": "GET", "url": "https://t/api/profile/{id}"}],
        "forms": [{"method": "POST", "action": "/feedback", "inputs": [{"name": "comment", "type": "text"}]}],
        "params": ["q", "sort"],
    }
    docs = {"operational_recipes": [{"name": "import", "steps": [
        {"method": "POST", "path": "/api/import", "body_sketch": {"bpmn": "...", "name": "..."}}]}]}
    inv = features.build_inventory(app_model, surface, docs)
    ids = {f["id"] for f in inv}
    assert "GET:/api/search" in ids and "POST:/api/items" in ids
    assert "POST:/feedback" in ids and "POST:/api/import" in ids
    assert "GET:/api/profile/{id}" in ids
    # path param extracted
    prof = next(f for f in inv if f["id"] == "GET:/api/profile/{id}")
    assert {"name": "id", "location": "path"} in prof["inputs"]
    # form input captured
    fb = next(f for f in inv if f["id"] == "POST:/feedback")
    assert any(i["name"] == "comment" for i in fb["inputs"])
    # docs body_sketch keys captured
    imp = next(f for f in inv if f["id"] == "POST:/api/import")
    assert {"name": "bpmn", "location": "body"} in imp["inputs"]


def test_search_feature_prioritised_via_cue_and_sink_hint():
    app_model = {
        "features": [{"name": "s", "endpoints": ["GET /api/search", "GET /api/health"]}],
        "trust_boundaries": ["SQL built from input on the search endpoint"],
    }
    inv = features.build_inventory(app_model, {}, {})
    # /api/search (cue 'search' + sink hint) ranks above /api/health
    assert inv[0]["id"] == "GET:/api/search"
    assert inv[0]["prio"] > next(f for f in inv if "health" in f["id"])["prio"]


def test_input_bearing_and_sweep_candidates():
    assert features.input_bearing({"method": "POST", "inputs": []}) is True   # state-changing
    assert features.input_bearing({"method": "GET", "inputs": [{"name": "q", "location": "query"}]}) is True
    assert features.input_bearing({"method": "GET", "inputs": []}) is False
    inv = [{"id": "a", "method": "POST", "inputs": [], "prio": 5},
           {"id": "b", "method": "GET", "inputs": [], "prio": 9}]
    cand = features.sweep_candidates(inv)
    assert [c["id"] for c in cand] == ["a"]   # only the input-bearing one


def test_classify_reflection_each_class():
    tok = "abc123"
    canary = features.polyglot_canary(tok, oob="http://oob/x")
    assert "<svg/onload=scx>" in canary and "1337*1337" in canary
    # xss: our tag bounced back unencoded
    assert "xss" in features.classify_reflection(tok, f"<div>{features._XSS_MARK}</div>")
    # xss NOT flagged when encoded
    assert "xss" not in features.classify_reflection(tok, "<div>&lt;svg/onload=scx&gt;</div>")
    # sqli: DB error fragment
    assert "sqli" in features.classify_reflection(tok, "ERROR: syntax error at or near \"'\"")
    # ssti: arithmetic evaluated, literal not echoed
    assert "ssti" in features.classify_reflection(tok, "result is 1787569 done")
    assert "ssti" not in features.classify_reflection(tok, "you sent {{1337*1337}}")
    # traversal: passwd content
    assert "traversal" in features.classify_reflection(tok, "root:x:0:0:root:/root:/bin/bash")
    # open-redirect: token in Location
    assert "open-redirect" in features.classify_reflection(tok, "", {"Location": f"http://evil/scx{tok}"})
    # ssrf/rce: OOB hit
    assert set(features.classify_reflection(tok, "200 ok", {}, [{"token": "x"}])) >= {"ssrf", "rce"}
    # nothing on a clean 200
    assert features.classify_reflection(tok, "<html>welcome</html>") == []


def test_internal_reach_oracle():
    """SSRF in-band oracle: an internal target reached past the egress filter (server-emitted reach
    signal) is confirmed; the cluster-block 403 alone is not; an internal target with no reach
    signal is not."""
    # reached: [::1] internal target + a backend reach signal
    ok, ev = features.internal_reach("http://[::1]:8080/actuator/env -> 401 INVALID_TOKEN")
    assert ok and "internal SSRF reach" in ev
    assert features.internal_reach("http://[::1]:8080/api-docs -> 200 {\"openapi\":\"3.0\"}")[0] is True
    assert features.internal_reach("169.254.169.254 -> ami-id i-0abc instance-identity")[0] is True
    # blocked: the cluster egress-denylist 403, no reach signal
    assert features.internal_reach("127.0.0.1 -> blocked in this cluster")[0] is False
    assert features.egress_blocked("HTTP calls to this domain are blocked in this cluster") is True
    # internal target addressed but only a generic 200 (no server reach signal) -> not confirmed
    assert features.internal_reach("http://[::1]:8080/ -> 200 OK")[0] is False
    # a reach signal with NO internal target addressed -> not confirmed (avoids false positives)
    assert features.internal_reach("public health page: \"status\":\"up\"")[0] is False


def test_class_objective_mapping():
    assert features.CLASS_OBJECTIVE["xss"] == "CLIENT-XSS-CSRF"
    assert features.CLASS_OBJECTIVE["sqli"] == "INFRA-RCE-INJECTION"
    assert features.CLASS_OBJECTIVE["traversal"] == "INFRA-PATH-TRAVERSAL"
    assert features.CLASS_OBJECTIVE["ssrf"] == "INFRA-SSRF"


def test_feature_coverage_ledger():
    inv = [
        {"id": "POST:/feedback", "method": "POST", "path": "/feedback", "inputs": [{"name": "c", "location": "body"}], "prio": 5},
        {"id": "GET:/search", "method": "GET", "path": "/search", "inputs": [{"name": "q", "location": "query"}], "prio": 4},
        {"id": "POST:/admin", "method": "POST", "path": "/admin", "inputs": [{"name": "x", "location": "body"}], "prio": 3},
        {"id": "GET:/health", "method": "GET", "path": "/health", "inputs": [], "prio": 0},  # not input-bearing
    ]
    probed = [
        {"feature_id": "POST:/feedback", "status": "triaged", "classes": ["xss"]},
        {"feature_id": "GET:/search", "status": "clean", "classes": []},
        {"feature_id": "POST:/admin", "status": "blocked", "reason": "cap"},
    ]
    ops = [{"feature_id": "POST:/feedback", "type": "objective_attempt"}]
    cov = features.feature_coverage(inv, probed, ops)
    assert cov["input_bearing"] == 3            # health excluded
    assert cov["with_signal"] == 1              # feedback (also deep-exploited)
    assert cov["deep_exploited"] == 1
    assert cov["blocked"] == 1
    assert cov["triaged"] == 2                  # feedback + search (admin blocked)
    fb = next(p for p in cov["per_feature"] if p["id"] == "POST:/feedback")
    assert fb["status"] == "deep-exploited"


def test_definition_field_features_from_playbook():
    pb = {"primitives": [
        {"task_type": "HTTP", "objective": "INFRA-SSRF", "how": "uri = sc.oob()"},
        {"task_type": "INLINE", "objective": "INFRA-RCE-INJECTION", "how": "JS that evaluates"},
        {"task_type": "SIMPLE/worker", "objective": "INTEG-WORKFLOW-STATE", "how": "queue"},  # not injection -> skipped
    ]}
    feats = features.definition_field_features(pb)
    ids = {f["id"]: f for f in feats}
    assert "WFDEF:HTTP:uri" in ids and ids["WFDEF:HTTP:uri"]["class_hint"] == "ssrf"
    assert "WFDEF:INLINE:expression" in ids and ids["WFDEF:INLINE:expression"]["class_hint"] == "eval"
    assert not any("SIMPLE" in i for i in ids)              # non-injection primitive skipped
    assert all(any(inp["location"] == "definition" for inp in f["inputs"]) for f in feats)
    # merged into the inventory via build_inventory(playbook=...)
    inv = features.build_inventory({}, {}, {}, pb)
    assert "WFDEF:INLINE:expression" in {f["id"] for f in inv}


def test_definition_field_sweep_seeds_deep_probe():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
    from common import feature_exercise as fx
    from common import deepen
    inv = features.definition_field_features({"primitives": [
        {"task_type": "INLINE", "objective": "INFRA-RCE-INJECTION", "how": "JS eval"}]})
    hyps = fx.feature_sweep_hypotheses([], inv, 2, {"userA": {"value": "a"}})
    assert len(hyps) == 1
    h = hyps[0]
    assert h["mandatory_kind"] == "feature_sweep" and h["sweep_class"] == "eval"
    assert "INLINE" in h["title"] and "workflow-definition" in h["target"]
    assert deepen.ladder_for(h)[0] == "js-sandbox-escape"   # eval routes to the JS ladder


def test_feature_sweep_hypotheses_seed():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
    from common import feature_exercise as fx
    inv = [{"id": "POST:/feedback", "method": "POST", "path": "/feedback",
            "inputs": [{"name": "comment", "location": "body"}], "prio": 5}]
    signals = [{"feature_id": "POST:/feedback", "field": "comment", "class": "xss", "evidence": "tag reflected"}]
    ids = {"anon": {}, "userA": {"value": "a"}, "userB": {"value": "b"}}
    hyps = fx.feature_sweep_hypotheses(signals, inv, 2, ids)
    assert len(hyps) == 1
    h = hyps[0]
    assert h["mandatory"] is True and h["mandatory_kind"] == "feature_sweep"
    assert h["objective_id"] == "CLIENT-XSS-CSRF" and h["category"] == "xss"
    assert len(h["identities"]) == 2           # stored-XSS needs a victim identity
    # routes to the xss ladder
    from common import deepen
    assert deepen.ladder_for(h)[0] == "xss"
