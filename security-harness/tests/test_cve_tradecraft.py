"""CVE exploitation tradecraft (§14): a version-matched CVE lead resolves to a concrete exploit
HINT (class + technique + oracle), routes to the dedicated CVE deepen ladder, and the MAND-CVE
hypothesis carries the HOW + the sc.cve_attempt tagging instruction."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
from common import cve_tradecraft as ct  # noqa: E402
from common import deepen, feature_exercise as fx  # noqa: E402


def test_hint_resolves_override_then_classify_then_generic():
    ct.load.cache_clear()
    # 1) curated per-CVE override
    h = ct.hint("CVE-2026-44249", "io.netty:netty-handler@4.1.133.Final")
    assert h["class"] == "ssrf-filter-bypass"
    assert "IPv6" in h["technique"] and "169.254.169.254" in h["oracle"]
    # 2) unmapped CVE classified from the advisory summary keywords
    h2 = ct.hint("CVE-9999-0001", "com.example:thing@1.0", summary="Insecure deserialization of user input")
    assert h2["class"] == "deserialization-gadget"
    # 3) nothing matches -> generic template (still gives a technique + oracle, never empty)
    h3 = ct.hint("CVE-9999-0002", "com.example:obscure@1.0", summary="some unrelated advisory text")
    assert h3["class"] == "generic" and h3["technique"] and h3["oracle"]
    # one-line hint is non-empty and names the class
    assert ct.hint_line("CVE-2026-44249", "io.netty:netty-handler@4.1.133.Final").startswith("TECHNIQUE [ssrf-filter-bypass]")


def test_cve_hypothesis_routes_to_the_cve_ladder():
    fam, ladder = deepen.ladder_for({"cve_id": "CVE-2026-44249", "objective_id": "INFRA-SUPPLY-CHAIN",
                                     "category": "cve", "title": "Attempt CVE-2026-44249 against netty-handler"})
    assert fam == "cve"
    families = [r["family"] for r in ladder]
    assert "published-poc" in families and "oob-confirm" in families
    # a CVE whose title mentions 'sql' must NOT be mis-routed to the SQLi ladder
    fam2, _ = deepen.ladder_for({"cve_id": "CVE-1", "title": "Attempt CVE-1 in the sql archive DAO"})
    assert fam2 == "cve"


def test_mand_cve_hypothesis_carries_exploit_hint_and_tagging_instruction():
    leads = [{"dependency": "io.netty:netty-handler@4.1.133.Final", "version_known": True,
              "priority_score": 2.0, "top_cves": [{"id": "CVE-2026-44249", "severity": "high"}]}]
    hyps = fx.mandatory_hypotheses({}, [], leads, {}, 2, {"anon": {"value": "x"}}, [], [])
    cve = next(h for h in hyps if h.get("mandatory_kind") == "cve")
    assert cve["category"] == "cve" and cve["cve_id"] == "CVE-2026-44249"
    assert isinstance(cve.get("exploit_hint"), dict) and cve["exploit_hint"]["class"] == "ssrf-filter-bypass"
    plan = " ".join(cve["test_plan"])
    assert "sc.cve_attempt(" in plan                       # the tagging instruction (was missing)
    assert "IPv6" in plan or "subnet" in plan.lower()       # the concrete technique, not just the id
    assert "169.254.169.254" in cve["expected_evidence"]    # the oracle drives expected evidence
