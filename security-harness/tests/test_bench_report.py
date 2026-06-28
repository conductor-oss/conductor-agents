"""Oracle-readiness report (design §19.2 / P3-4): the canonical coverage corpus must measure
every catalog class (the 'a class isn't covered until measured' guarantee — E10), and
``coverage.build_report`` must render the coverage / adversarial / held-out sections."""
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "bench"))
import coverage as cov_mod  # noqa: E402
import score as score_mod  # noqa: E402

EXPECTED_DIR = os.path.join(ROOT, "bench", "expected")
CATALOG = os.path.join(ROOT, "catalog", "objectives.yaml")


def test_catalog_coverage_corpus_measures_every_objective():
    """If this fails, a catalog objective was added without a coverage fixture — so HC would
    auto-tune an unmeasured class. Add a fixture to bench/expected/catalog-coverage.json."""
    catalog = cov_mod._load_catalog(CATALOG)
    positives, _ = cov_mod.load_fixtures(EXPECTED_DIR)
    c = score_mod.objective_coverage(positives, catalog)
    assert c["unmeasured"] == [], f"unmeasured catalog objectives: {c['unmeasured']}"
    assert c["measured"] == c["total"] and c["pct"] == 1.0


def test_load_fixtures_splits_positive_and_negative():
    positives, negatives = cov_mod.load_fixtures(EXPECTED_DIR)
    assert positives and all(p.get("kind") != "negative" for p in positives)
    assert negatives and all(n.get("kind") == "negative" for n in negatives)


def test_build_report_flags_unmeasured_and_split_shortfall():
    catalog = [{"id": "A", "class": "x"}, {"id": "B", "class": "y"}]
    positives = [{"objective_id": "A", "class": "x", "id": "pos-a"}]
    negatives = [{"id": "neg-1", "class": "x", "why": "looks bad, isn't"}]
    targets = [{"name": "t1", "expected": "e1.json"}]            # only 1 scored target
    md = cov_mod.build_report(catalog, positives, negatives, targets, k=2)
    for section in ("Oracle coverage", "Per-class fixture inventory",
                    "Adversarial corpus", "Held-out split"):
        assert section in md
    assert "B" in md and "Unmeasured" in md                      # B has no fixture -> do not tune
    assert "needs >=2" in md                                     # 1 scored target -> warning


def test_build_report_full_coverage_and_genuine_split():
    catalog = [{"id": "A", "class": "x"}]
    positives = [{"objective_id": "A", "class": "x", "id": "pos-a"}]
    targets = [{"name": "t1", "expected": "e1"}, {"name": "t2", "expected": "e2"}]
    md = cov_mod.build_report(catalog, positives, [], targets, k=2)
    assert "Unmeasured: none" in md
    assert "Fold" in md and "needs >=2" not in md                # 2 scored -> a real fold table
