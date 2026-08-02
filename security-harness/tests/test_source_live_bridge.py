"""Source-finding -> live-hypothesis bridge (item #5): leaked secrets, exposed management
endpoints, and formatted-SQL DAOs become ACTIVE live hypotheses instead of dead-ending in the
report."""
from common import feature_exercise as fe

_F2 = {"title": "Default JWT signing secret committed in production configuration",
       "category": "hardcoded-secret", "severity": "High"}
_F4 = {"title": "Spring Boot Actuator fully enabled (sensitive endpoints)",
       "category": "misconfig", "evidence": "actuator env heapdump", "severity": "High"}
_F1 = {"title": "Possible SQL injection via string-formatted SQL in archive DAOs",
       "category": "sqli", "evidence": "Detected a formatted string in a SQL statement"}
# formatted-SQL finding with NO generic 'inject'/'sql injection' token in its text:
_FMT = {"title": "Formatted-SQL DAO", "evidence": "formatted-sql", "category": "dao"}


def test_secret_leads_classifies_committed_jwt_secret():
    assert fe.secret_leads([_F2]) == [_F2]
    assert fe.secret_leads([_F4]) == []


def test_exposed_mgmt_leads_classifies_actuator():
    assert fe.exposed_mgmt_leads([_F4]) == [_F4]
    assert fe.exposed_mgmt_leads([_F2]) == []


def test_formatted_sql_now_recognized_as_injection_sink():
    # Regression: a "Formatted-SQL DAO" with no 'inject' token was previously missed by injection_sinks.
    assert fe.injection_sinks([_FMT]) == [_FMT]
    assert fe.injection_sink_class(_FMT) == "sqli"


def _hyps(sast):
    return fe.mandatory_hypotheses({}, [], [], {}, 2, {"userA": {}}, sast_findings=sast)


def test_leaked_secret_generates_forge_hypothesis():
    hyps = {h["id"]: h for h in _hyps([_F2])}
    assert "MAND-SECRET-USE" in hyps
    assert hyps["MAND-SECRET-USE"]["objective_id"] == "INFRA-SECRET-SURFACE"


def test_actuator_generates_probe_hypothesis():
    assert "MAND-MGMT-PROBE" in {h["id"] for h in _hyps([_F4])}


def test_secret_surface_pending_when_lead_present():
    status = fe.evaluate({}, [], [], {}, 2, None, None, [_F2])
    assert "INFRA-SECRET-SURFACE" in status["pending"]
    assert "INFRA-SECRET-SURFACE" in status["required"]


def test_secret_surface_blocked_below_capability_2():
    status = fe.evaluate({}, [], [], {}, 1, None, None, [_F2])
    assert any(b["id"] == "INFRA-SECRET-SURFACE" for b in status["blocked"])


def test_no_secret_hypothesis_without_lead():
    ids = {h["id"] for h in _hyps([_F1])}
    assert "MAND-SECRET-USE" not in ids and "MAND-MGMT-PROBE" not in ids
