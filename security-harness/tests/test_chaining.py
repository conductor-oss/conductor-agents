"""Predictive mid-run chaining (§5 Phase 4a): a confirmed finding's unlocked precondition becomes a
FORCED mandatory hypothesis that USES the confirmed material to escalate (cascade), instead of being
left as a textual reflect suggestion."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
from common import chaining, feature_exercise as fx  # noqa: E402


def test_confirmed_findings_force_chained_hypotheses():
    confirmed = [
        {"objective_id": "INFRA-SSRF", "category": "ssrf", "title": "SSRF via HTTP task"},
        {"objective_id": "CONF-CROSS-TENANT-READ", "category": "cross-tenant", "title": "read tenantB data"},
    ]
    chained = chaining.chained_hypotheses(confirmed, ["tenantA", "tenantB"])
    by_obj = {h["objective_id"]: h for h in chained}
    # SSRF (internal_reach) -> forced secret-surface chain that USES the SSRF primitive
    assert "INFRA-SECRET-SURFACE" in by_obj
    ssrf_chain = by_obj["INFRA-SECRET-SURFACE"]
    assert ssrf_chain["mandatory"] is True and ssrf_chain["mandatory_kind"] == "chain"
    assert "SSRF via HTTP task" in " ".join(ssrf_chain["test_plan"])         # uses the confirmed material
    assert "169.254.169.254" in " ".join(ssrf_chain["test_plan"])            # concrete escalation target
    # cross-tenant read -> forced cross-tenant WRITE (cascade: read is the floor, not the ceiling)
    assert by_obj["INTEG-CROSS-WRITE"]["mandatory_kind"] == "chain"
    assert "WRITE" in " ".join(by_obj["INTEG-CROSS-WRITE"]["test_plan"])


def test_no_confirmed_findings_means_no_chain_hypotheses():
    assert chaining.chained_hypotheses([], ["anon"]) == []
    assert chaining.chained_hypotheses(None, None) == []


def test_mandatory_hypotheses_appends_chained_from_prior_confirmed():
    prior = [{"objective_id": "INFRA-SSRF", "category": "ssrf", "title": "SSRF primitive confirmed"}]
    hyps = fx.mandatory_hypotheses({}, [], [], {}, 2, {"anon": {"value": "x"}}, [], [], None,
                                   prior_confirmed=prior)
    chain = [h for h in hyps if h.get("mandatory_kind") == "chain"]
    assert chain and any(h["id"] == "MAND-CHAIN-internal_reach" for h in chain)
