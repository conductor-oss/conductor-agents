"""Identity-adequacy gate (ROADMAP E1) + coverage blocking."""
from common import coverage, identity


def test_single_identity_cannot_prove_cross_tenant():
    a = identity.classify({"anon": {}, "app": {"value": "tok"}})
    assert a["authenticated"] is True
    assert a["cross_user"] is False        # need 2 principals
    assert a["cross_tenant"] is False      # need 2 tenants
    assert "NOT provable" in a["note"]


def test_two_identities_enable_cross_user_and_tenant():
    a = identity.classify({"anon": {}, "userA": {"value": "a"}, "userB": {"value": "b"}})
    assert a["cross_user"] is True
    assert a["cross_tenant"] is True       # distinct identities assumed distinct tenants
    assert a["tenants_explicit"] is False


def test_explicit_tenant_tags_counted():
    a = identity.classify({"app1": {"value": "a", "tenant": "t1"}, "app2": {"value": "b", "tenant": "t1"}})
    # both in the SAME tenant -> cross-tenant NOT provable despite 2 identities
    assert a["distinct_tenants"] == 1
    assert a["cross_tenant"] is False


def test_blocked_by_adequacy_mapping():
    inadequate = identity.classify({"anon": {}, "app": {"value": "t"}})  # 1 priv
    assert identity.blocked_by_adequacy(">=2 distinct-tenant identities", inadequate) is True
    assert identity.blocked_by_adequacy(">=2 users", inadequate) is True
    assert identity.blocked_by_adequacy("1 user", inadequate) is False    # no >=2 requirement


def test_coverage_blocks_cross_tenant_with_one_identity():
    applicable = [
        {"id": "CONF-CROSS-TENANT-READ", "class": "tenancy", "objective": "read another tenant",
         "coverage_dimension": "tenant_isolation", "required_identities": ">=2 distinct-tenant identities",
         "refs": {"owasp": "A01", "cwe": "CWE-639"}},
        {"id": "INFRA-SECRET-SURFACE", "class": "infra", "objective": "harvest secrets",
         "coverage_dimension": "infra", "required_identities": "1 user", "refs": {"owasp": "A05", "cwe": "CWE-200"}},
    ]
    adequacy = identity.classify({"anon": {}, "app": {"value": "t"}})  # 1 privileged
    res = coverage.build_from_catalog(applicable, [], confirmed=[], tried=[], rejected=[], adequacy=adequacy)
    by = {c["objective_id"]: c["status"] for c in res["ledger"]}
    assert by["CONF-CROSS-TENANT-READ"] == "blocked"     # NOT untested/clean
    assert by["INFRA-SECRET-SURFACE"] == "untested"       # no identity requirement -> normal


def test_confirmed_finding_overrides_block():
    applicable = [{"id": "CONF-CROSS-TENANT-READ", "class": "tenancy", "objective": "x",
                   "coverage_dimension": "tenant_isolation",
                   "required_identities": ">=2 distinct-tenant identities", "refs": {}}]
    adequacy = identity.classify({"app": {"value": "t"}})
    confirmed = [{"title": "cross tenant", "objective_id": "CONF-CROSS-TENANT-READ"}]
    res = coverage.build_from_catalog(applicable, [], confirmed, tried=[], rejected=[], adequacy=adequacy)
    assert res["ledger"][0]["status"] == "tested"   # actually exercised wins over 'blocked'
