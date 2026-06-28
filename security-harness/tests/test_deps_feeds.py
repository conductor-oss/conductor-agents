"""Multi-feed CVE identity (P1-2): OSV + GHSA identity feeds merged, NVD severity enrichment."""
from common import deps

DEPS = [{"ecosystem": "Maven", "name": "pkg", "version": "1.0", "scope": "compile"}]


def _fake_osv(eco, name, ver):  # OSV: server-side version-matched
    return [{"id": "CVE-1", "severity": "high", "summary": "osv"}] if name == "pkg" else []


def _fake_ghsa(eco, name, ver):  # GHSA: package advisories (one dup of OSV, one OSV missed)
    return ([{"id": "CVE-1", "severity": "critical", "summary": "ghsa"},
             {"id": "CVE-2", "severity": "moderate", "summary": "ghsa2"}] if name == "pkg" else [])


def test_osv_is_version_matched():
    r = deps.query_osv(DEPS, _fake_osv)
    assert r and r[0]["version_known"] is True and r[0]["top"][0]["id"] == "CVE-1"


def test_ghsa_is_version_unknown_historical():
    r = deps.query_ghsa(DEPS, _fake_ghsa)
    assert r and r[0]["version_known"] is False   # GHSA = historical lead, never auto-attempted (D10)


def test_ghsa_degrades_to_empty_without_results():
    assert deps.query_ghsa(DEPS, lambda e, n, v: []) == []


def test_merge_unions_and_dedupes_by_cve_id():
    merged = deps.merge_cve_records(deps.query_osv(DEPS, _fake_osv), deps.query_ghsa(DEPS, _fake_ghsa))
    assert len(merged) == 1                                   # same dependency, one row
    assert {c["id"] for c in merged[0]["top"]} == {"CVE-1", "CVE-2"}   # union; CVE-1 deduped
    assert merged[0]["version_known"] is True                 # OSV matched -> version_known OR
    cve1 = next(c for c in merged[0]["top"] if c["id"] == "CVE-1")
    assert cve1["severity"] == "critical"                     # higher severity wins for the shared CVE


def test_nvd_enrich_backfills_missing_severity():
    recs = [{"dependency": "p@1", "ecosystem": "Maven", "scope": "compile", "version_known": True,
             "cve_count": 1, "top": [{"id": "CVE-9", "severity": "", "summary": "x"}]}]
    deps.nvd_enrich(recs, lambda cid: {"severity": "high", "cvss": 7.5} if cid == "CVE-9" else None)
    assert recs[0]["top"][0]["severity"] == "high" and recs[0]["top"][0]["cvss"] == 7.5


def test_nvd_enrich_skips_non_cve_ids():
    recs = [{"top": [{"id": "GHSA-xxxx", "severity": ""}]}]
    called = []
    deps.nvd_enrich(recs, lambda cid: called.append(cid))
    assert called == []                                       # NVD is by CVE id only
