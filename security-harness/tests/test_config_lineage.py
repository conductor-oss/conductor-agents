"""Config versioning & lineage — the §19 H5 substrate for hill climbing.

Each test asserts one property the architecture requires of config lineage:
versioned, content-addressed, provenance-attributed, benchmark-scored before/after,
reproducible (pinned model/bench/seed), reversible, and tamper-evident.
"""
from common import config_lineage as cl

T0 = "2026-06-21T00:00:00Z"


def test_content_addressed_and_stable():
    # Content-address: same content -> same hash; changed content -> different hash (§19.8).
    a = cl.content_hash("you are a verifier. refute by default.")
    assert a == cl.content_hash("you are a verifier. refute by default.")
    assert a != cl.content_hash("you are a verifier. confirm by default.")


def test_versions_are_monotonic_per_surface_path():
    store = []
    cl.commit(store, surface="prompt", path="prompts/verify.md", content="v1", as_of=T0)
    cl.commit(store, surface="prompt", path="prompts/verify.md", content="v2", as_of=T0)
    # A different path keeps its own version line.
    cl.commit(store, surface="catalog", path="catalog/objectives.yaml", content="c1", as_of=T0)
    assert [r["version"] for r in cl.lineage(store, "prompt", "prompts/verify.md")] == [1, 2]
    assert cl.head(store, "prompt", "prompts/verify.md")["version"] == 2
    assert cl.head(store, "catalog", "catalog/objectives.yaml")["version"] == 1
    assert cl.next_version(store, "prompt", "prompts/verify.md") == 3


def test_provenance_pins_and_benchmark_scores_round_trip():
    # H5: attributed to motivating traces + benchmark-scored before/after; §19.8: pinned repro.
    store = []
    rec = cl.commit(
        store, surface="prompt", path="prompts/hypothesize.md", content="v2", as_of=T0,
        model="claude-opus-4-8", seed=1234, benchmark="bench@v5",
        provenance={"proposal_id": "p-7", "diagnosis": "FN on CONF-CROSS-TENANT-READ",
                    "traces": ["run-a", "run-b"]},
        scores={"before": {"recall": 0.60}, "after": {"recall": 0.75}},
    )
    assert rec["model"] == "claude-opus-4-8" and rec["seed"] == 1234 and rec["benchmark"] == "bench@v5"
    assert rec["provenance"]["traces"] == ["run-a", "run-b"]
    assert rec["scores"]["after"]["recall"] == 0.75
    # parent links to the (here absent) prior content -> None for the first edition.
    assert rec["parent_content_hash"] is None
    # why() reproduces "why this edition" with the benchmark delta + diagnosis (§19.8).
    expl = cl.why(rec)
    assert "FN on CONF-CROSS-TENANT-READ" in expl and "0.6" in expl and "0.75" in expl


def test_parent_links_successive_editions():
    store = []
    r1 = cl.commit(store, surface="profile", path="profiles/vuln-app.json", content="p1", as_of=T0)
    r2 = cl.commit(store, surface="profile", path="profiles/vuln-app.json", content="p2", as_of=T0)
    assert r2["parent_content_hash"] == r1["content_hash"]


def test_rollback_target_is_the_prior_version():
    store = []
    r1 = cl.commit(store, surface="prompt", path="prompts/exploit.md", content="v1", as_of=T0)
    cl.commit(store, surface="prompt", path="prompts/exploit.md", content="v2", as_of=T0)
    # H5 automatic rollback: revert head -> the prior edition.
    target = cl.rollback_target(store, "prompt", "prompts/exploit.md")
    assert target["version"] == 1 and target["content_hash"] == r1["content_hash"]
    # A surface with only its initial version has nothing to roll back to.
    assert cl.rollback_target(store, "prompt", "prompts/never-changed.md") is None


def test_chain_verifies_and_detects_tampering():
    store = []
    cl.commit(store, surface="catalog", path="catalog/objectives.yaml", content="c1", as_of=T0)
    cl.commit(store, surface="prompt", path="prompts/reflect.md", content="r1", as_of=T0)
    cl.commit(store, surface="profile", path="profiles/vuln-app.json", content="p1", as_of=T0)
    assert cl.verify_chain(store) == {"ok": True, "entries": 3, "broken_at": None}
    # Silently edit a committed edition's content_hash -> the chain must break at that entry.
    store[1]["content_hash"] = "deadbeef"
    v = cl.verify_chain(store)
    assert v["ok"] is False and v["broken_at"] == 1


def test_safety_authz_is_not_a_tunable_surface():
    # §19.4 / H2: safety/authz is NEVER tunable, so it cannot get an edition.
    import pytest
    with pytest.raises(ValueError):
        cl.make_edition(surface="safety", path="authz", content="x", as_of=T0)


def test_snapshot_baselines_the_live_config_tree():
    # Proves the sub-item: the REAL catalog/prompts/profiles become content-addressed,
    # versioned, tamper-evident editions (H5: "the catalog, prompts, and profiles are all versioned").
    import os, glob
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    items = [("catalog", "catalog/objectives.yaml",
              open(os.path.join(repo, "catalog/objectives.yaml")).read())]
    for p in sorted(glob.glob(os.path.join(repo, "prompts", "*.md"))):
        items.append(("prompt", os.path.relpath(p, repo), open(p).read()))
    for p in sorted(glob.glob(os.path.join(repo, "profiles", "*.json"))):
        items.append(("profile", os.path.relpath(p, repo), open(p).read()))

    store = []
    recs = cl.snapshot(store, items, as_of=T0, model="claude-opus-4-8", benchmark="bench@v0")
    assert len(recs) == len(items) >= 3                       # catalog + many prompts + profile(s)
    assert cl.verify_chain(store)["ok"] is True               # the whole live config tree is one verifiable lineage
    assert all(r["version"] == 1 and len(r["content_hash"]) == 64 for r in recs)
    # the catalog really is in there, content-addressed
    assert cl.head(store, "catalog", "catalog/objectives.yaml") is not None
