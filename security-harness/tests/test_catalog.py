"""Security Objective Catalog (ROADMAP E0) — the harness spine."""
from common import catalog, coverage


def test_repo_catalog_loads_and_is_well_formed():
    cat = catalog.load()
    assert len(cat) >= 20
    ids = set()
    for e in cat:
        for field in ("id", "class", "objective", "coverage_dimension", "refs"):
            assert e.get(field), f"{e.get('id')} missing {field}"
        assert e["id"] not in ids, f"duplicate id {e['id']}"
        ids.add(e["id"])
        assert {"owasp", "cwe"} <= set(e["refs"].keys())
    # the three customer-named pillars are represented
    assert "CONF-CROSS-TENANT-READ" in ids        # data leak / cross-tenant
    assert "AVAIL-COMPLEXITY" in ids               # destabilize
    assert "INFRA-SECRET-SURFACE" in ids           # infra secrets


def test_applicable_grammar():
    facts = {"multi_tenant": True, "has_browser_ui": False}
    assert catalog.applicable({"applicable_when": "always"}, facts) is True
    assert catalog.applicable({"applicable_when": "multi_tenant"}, facts) is True
    assert catalog.applicable({"applicable_when": "has_browser_ui"}, facts) is False
    assert catalog.applicable({"applicable_when": ["multi_tenant", "has_browser_ui"]}, facts) is False
    assert catalog.applicable({"applicable_when": "has_browser_ui", "any_of": ["multi_tenant"]}, facts) is True


def test_derive_facts_heuristics_and_explicit_override():
    am = {"purpose": "multi-tenant workspace platform", "facts": {"handles_payments": False}}
    f = catalog.derive_facts(am, extra={"has_source": True})
    assert f["multi_tenant"] is True            # inferred from "multi-tenant"/"workspace"
    assert f["handles_payments"] is False       # explicit override wins
    assert f["has_source"] is True              # runtime extra


def test_applicable_partition_and_na():
    cat = catalog.load()
    facts = {"multi_tenant": False, "has_outbound_fetch": False, "has_browser_ui": False,
             "handles_payments": False, "has_source": False, "authenticated": False}
    appl = catalog.applicable_entries(cat, facts)
    na = catalog.na_entries(cat, facts)
    assert len(appl) + len(na) == len(cat)
    na_ids = {e["id"] for e in na}
    # cross-tenant is N/A when not multi-tenant; SSRF N/A without outbound fetch
    assert "CONF-CROSS-TENANT-READ" in na_ids
    assert "INFRA-SSRF" in na_ids
    # always-applicable objectives are present regardless
    assert any(e["id"] == "INFRA-RCE-INJECTION" for e in appl)


def test_coverage_from_catalog_classifies():
    applicable = [
        {"id": "CONF-BOLA-CROSS-USER", "class": "authz", "objective": "read another user's object",
         "coverage_dimension": "object_authz", "refs": {"owasp": "A01", "cwe": "CWE-639"}},
        {"id": "INFRA-SSRF", "class": "infra", "objective": "reach internal via ssrf",
         "coverage_dimension": "infra", "refs": {"owasp": "A10", "cwe": "CWE-918"}},
        {"id": "AUTH-JWT-FLAW", "class": "crypto", "objective": "forge a token",
         "coverage_dimension": "identity", "refs": {"owasp": "A02", "cwe": "CWE-347"}},
    ]
    not_applicable = [{"id": "CLIENT-XSS-CSRF", "class": "client", "coverage_dimension": "client"}]
    confirmed = [{"title": "BOLA", "objective_id": "CONF-BOLA-CROSS-USER"}]   # exact id match -> tested
    tried = ["ssrf|/api/x|app"]                                              # SSRF only attempted -> partial via id? no id; token
    res = coverage.build_from_catalog(applicable, not_applicable, confirmed, tried, rejected=[])
    by = {c["objective_id"]: c["status"] for c in res["ledger"]}
    assert by["CONF-BOLA-CROSS-USER"] == "tested"
    assert by["AUTH-JWT-FLAW"] == "untested"
    assert by["CLIENT-XSS-CSRF"] == "not_applicable"
    assert res["summary"]["by_status"].get("not_applicable", 0) == 1


def test_compact_trims_verbose_fields():
    c = catalog.compact([{"id": "X", "class": "infra", "objective": "o", "invariant": "long",
                          "how_to_test": "h", "coverage_dimension": "infra", "refs": {}}])
    assert "invariant" not in c[0] and c[0]["id"] == "X"
