"""Assertion provenance types (§2 principle 7, §12): documented/source/inferred/observed."""
from common import provenance as prov
from common import findings
from common import memory


def test_classify_maps_each_producer_to_its_kind():
    assert prov.classify("docs_digest") == prov.DOCUMENTED
    assert prov.classify("semgrep") == prov.SOURCE
    assert prov.classify("dep_cve_scan") == prov.SOURCE
    assert prov.classify("route_extract") == prov.SOURCE
    assert prov.classify("recon") == prov.INFERRED
    assert prov.classify("http_request") == prov.OBSERVED      # live behavior
    assert prov.classify("") == prov.OBSERVED                  # safe default


def test_finding_tags_provenance_from_source_tool():
    assert findings.finding(title="x", source_tool="semgrep")["provenance"] == "source"
    assert findings.finding(title="y", source_tool="exploit_agent")["provenance"] == "observed"
    # an explicit provenance overrides the inferred default
    assert findings.finding(title="z", source_tool="semgrep", provenance="documented")["provenance"] == "documented"


def test_memory_stamp_preserves_distinct_provenance():
    # A documented assertion stays 'documented' through persistence (not overwritten to observed).
    f = memory._stamp({"title": "doc says tenants are isolated", "provenance": "documented"},
                      "fp-1", "2026-06-21T00:00:00Z")
    assert f["provenance"] == "documented"
    # an untagged finding defaults to observed
    assert memory._stamp({"title": "live 200"}, "fp-1", "2026-06-21T00:00:00Z")["provenance"] == "observed"
