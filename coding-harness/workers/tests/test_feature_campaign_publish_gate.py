"""A campaign stopped before implementation parked on a human PR gate for nothing (review of PR 16)."""
import json
import os

from common import feature_campaign as fc


def test_publication_plan_says_whether_there_is_a_draft_to_approve():
    plan = lambda **kw: fc.resolve_campaign_publication_plan(**{  # noqa: E731
        "outcome": "verified", "requested_draft": False, "test_state": "tests_passed", "tests_passed": True,
        "tested": "abc123", "committed": "abc123", "agent_authored_test": False, **kw})["publish"]
    assert plan() == "yes" and plan(outcome="incomplete") == "yes"
    assert plan(tested="", committed="") == "no"          # nothing was committed
    assert plan(outcome="design_rejected") == "no"       # stopped before the end


def test_the_pr_gate_runs_only_when_there_is_something_to_publish():
    wf = json.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "feature_campaign.json")))
    refs = [t["taskReferenceName"] for t in wf["tasks"]]
    assert "campaign_pr_approval" not in refs
    gate = next(t for t in wf["tasks"] if t["taskReferenceName"] == "campaign_publish_needed_gate")
    assert gate["inputParameters"] == {"publish": "${campaign_publication_plan.output.publish}"}
    assert [t["taskReferenceName"] for t in gate["decisionCases"]["yes"]] == ["campaign_pr_approval",
                                                                            "record_campaign_pr_approval"]
    assert gate["defaultCase"][0]["inputParameters"] == {"prApprovalState": "suppressed"}
    assert refs.index("campaign_publish_needed_gate") < refs.index("campaign_publish_gate")
