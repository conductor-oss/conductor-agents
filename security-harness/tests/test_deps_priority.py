"""CVE real-world prioritization (§14, D10): KEV/EPSS/exploit-availability + reachability."""
import copy

from common import deps


def _two_known():
    # libssl: a MEDIUM CVE that is KEV-listed + has a public exploit + high EPSS.
    # libfoo: a HIGH CVE with none of those.
    return [
        {"dependency": "x:libssl@1.0", "ecosystem": "Maven", "version_known": True,
         "top": [{"id": "CVE-KEV", "severity": "medium", "summary": "rce"}]},
        {"dependency": "x:libfoo@2.0", "ecosystem": "Maven", "version_known": True,
         "top": [{"id": "CVE-PLAIN", "severity": "high", "summary": "xxe"}]},
    ]


def test_kev_and_exploit_beat_higher_cvss():
    recs = deps.prioritize(_two_known(), kev={"CVE-KEV"}, exploitable={"CVE-KEV"},
                           epss={"CVE-KEV": 0.9})
    assert recs[0]["dependency"].startswith("x:libssl")          # KEV+exploit+EPSS wins over plain-high
    assert recs[0]["kev"] is True and recs[0]["exploit_available"] is True
    assert deps.top_attempt(recs)["cve"]["id"] == "CVE-KEV"


def test_version_unknown_is_never_auto_attempted():
    recs = [
        {"dependency": "x:vuln@?", "ecosystem": "Maven", "version_known": False,
         "top": [{"id": "CVE-UNK", "severity": "critical"}]},          # KEV+exploit+critical, but UNKNOWN version
        {"dependency": "x:known@1.0", "ecosystem": "Maven", "version_known": True,
         "top": [{"id": "CVE-OK", "severity": "low"}]},
    ]
    recs = deps.prioritize(recs, kev={"CVE-UNK"}, exploitable={"CVE-UNK"})
    # D10: a version-unknown CVE is a lead to verify, never auto-exploited.
    assert deps.top_attempt(recs)["cve"]["id"] == "CVE-OK"
    assert recs[0]["version_known"] is True                       # known sorts ahead of unknown regardless


def test_reachable_boosts_priority_over_unknown_reachability():
    base = [{"dependency": "x:r@1.0", "ecosystem": "Maven", "version_known": True,
             "top": [{"id": "C1", "severity": "high"}]}]
    reach = deps.prioritize(copy.deepcopy(base), reachable={"x:r"})
    unknown = deps.prioritize(copy.deepcopy(base), reachable=None)
    assert reach[0]["priority_score"] > unknown[0]["priority_score"]
    assert reach[0]["reachable"] is True
