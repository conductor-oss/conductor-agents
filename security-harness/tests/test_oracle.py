"""The benchmark oracle (§19.2): living fixtures, held-out splits, adversarial negatives."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bench"))
import oracle  # noqa: E402
import score   # noqa: E402

T0 = "2026-06-21T00:00:00Z"


def test_kfold_is_balanced_holdout_disjoint_and_stable():
    items = list(range(10))
    splits = oracle.kfold(items, 5)
    assert len(splits) == 5
    for s in splits:                       # train and holdout partition the set, no overlap
        assert set(s["train"]) | set(s["holdout"]) == set(items)
        assert set(s["train"]) & set(s["holdout"]) == set()
    assert oracle.kfold(items, 5) == splits  # deterministic (no RNG) -> reproducible promotions


def test_living_fixture_distill_and_dedupe():
    f = {"title": "cross-tenant read on /api/workflows", "objective_id": "CONF-CROSS-TENANT-READ",
         "class": "tenancy", "category": "cross-tenant read", "content_hash": "abc123def456"}
    fx = oracle.distill(f, as_of=T0)
    assert fx["origin"] == "ratified" and fx["kind"] == "positive" and fx["as_of"] == T0
    assert fx["objective_id"] == "CONF-CROSS-TENANT-READ" and fx["keywords"]
    # forgetting guard: merging adds new, dedupes a re-distill of the same finding
    m1 = oracle.merge_fixtures([], [fx])
    assert m1["added"] == [fx["id"]]
    m2 = oracle.merge_fixtures(m1["corpus"], [oracle.distill(f, as_of=T0)])
    assert m2["added"] == [] and len(m2["corpus"]) == 1


def test_adversarial_negatives_are_precision_probes():
    fixtures = json.load(open(os.path.join(os.path.dirname(__file__), "..", "bench",
                                           "expected", "adversarial.json")))["fixtures"]
    # A harness that flags the near-miss negative ("returns the caller's own profile") fails precision.
    bad = [{"title": "data exposure: returns the caller's own profile", "category": "data exposure"}]
    s = score.score(fixtures, bad)
    assert s["precision_failures"]            # the trap was taken
    # recall is computed over POSITIVES only (negatives don't inflate the denominator)
    assert s["expected"] == len([f for f in fixtures if f.get("kind") != "negative"])
    # A clean harness (flags nothing) takes no trap and is not penalized on precision.
    assert score.score(fixtures, [])["precision_failures"] == []


def test_distill_findings_cli_core_gates_on_ratification(tmp_path):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bench"))
    import distill_findings as df
    into = str(tmp_path / "ratified.json")
    confirmed = [
        {"title": "SSRF via HTTP-task uri", "objective_id": "INFRA-SSRF", "class": "infra",
         "category": "ssrf", "content_hash": "abc123", "ratified": True,
         "evidence": "uri=http://[::1]:8080 reached internal services"},
        {"title": "unratified XXE", "objective_id": "INFRA-SSRF", "class": "infra",
         "category": "xxe", "content_hash": "def456"},   # NOT ratified
    ]
    # default: only the ratified one is distilled
    r = df.distill_findings(confirmed, ratify_all=False, into=into, as_of="t0")
    assert r["ratified"] == 1 and len(r["added"]) == 1
    assert any(fx["objective_id"] == "INFRA-SSRF" and fx["origin"] == "ratified" for fx in r["corpus"])
    # write it, then re-run --ratify-all: the already-present one dedupes, the XXE is added
    json.dump({"expected": r["corpus"]}, open(into, "w"))
    r2 = df.distill_findings(confirmed, ratify_all=True, into=into, as_of="t1")
    assert r2["ratified"] == 2 and len(r2["added"]) == 1   # only the new (xxe) added; ssrf deduped
    # nothing ratified -> no fixtures
    assert df.distill_findings([{"title": "x", "objective_id": "Y"}], ratify_all=False, into=into, as_of="t")["ratified"] == 0


def test_distill_ratified_reports_drain_hook_gates_and_persists(tmp_path):
    """The post-run ratification hook drains ONLY ratified findings across all dossiers, persists
    iff something new was added, and is forgetting-guarded (a second drain adds nothing)."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bench"))
    import distill_findings as df
    reports = tmp_path / "reports"
    (reports / "scanA").mkdir(parents=True)
    (reports / "scanB").mkdir(parents=True)
    json.dump({"confirmed_findings": [
        {"title": "SSRF via uri", "objective_id": "INFRA-SSRF", "category": "ssrf",
         "content_hash": "h1", "ratified": True, "evidence": "reached [::1]"},
        {"title": "unratified RCE", "objective_id": "INFRA-RCE-INJECTION", "content_hash": "h2"},
    ]}, open(reports / "scanA" / "dossier.json", "w"))
    json.dump({"confirmed_findings": [
        {"title": "XSS reflected", "objective_id": "CLIENT-XSS-CSRF", "category": "xss",
         "content_hash": "h3", "ratified": True, "evidence": "alert fired"},
    ]}, open(reports / "scanB" / "dossier.json", "w"))
    into = str(tmp_path / "ratified.json")

    res = df.distill_ratified_reports(str(reports), into=into, as_of="t0")
    assert res["scanned"] == 3 and res["ratified"] == 2 and len(res["added"]) == 2   # the unratified RCE excluded
    assert os.path.exists(into)                                                       # persisted (added > 0)
    corpus = json.load(open(into))["expected"]
    assert {fx["objective_id"] for fx in corpus} == {"INFRA-SSRF", "CLIENT-XSS-CSRF"}
    # forgetting guard: a second drain adds nothing new (dedupe by signature)
    res2 = df.distill_ratified_reports(str(reports), into=into, as_of="t1")
    assert res2["ratified"] == 2 and res2["added"] == []
    # an empty reports dir is a clean no-op (no crash, nothing written)
    empty = tmp_path / "empty"; empty.mkdir()
    out_missing = str(tmp_path / "none.json")
    res3 = df.distill_ratified_reports(str(empty), into=out_missing, as_of="t2")
    assert res3["scanned"] == 0 and res3["ratified"] == 0 and not os.path.exists(out_missing)
