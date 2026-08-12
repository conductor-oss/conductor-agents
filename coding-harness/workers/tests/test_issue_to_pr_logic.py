"""Unit tests for the logic that used to live in issue_to_pr's jq expressions."""

from __future__ import annotations

import pytest

from common import issue_to_pr

JUNK = (None, "", "  ", 0, True, [], {}, "a string", [None], {"nested": 1})


@pytest.mark.parametrize("value", JUNK)
def test_every_normalizer_never_raises(value):
    d = issue_to_pr.normalize_issue_delivery(child=value, publication_commit=value,
                                             workspace_state=value, fallback_commit=value)
    assert d["state"]
    v = issue_to_pr.normalize_verified_delivery(delivery=value, verification=value)
    assert "state" in v
    p = issue_to_pr.resolve_publication_plan(value)
    assert p["publish"] == "true"
    r = issue_to_pr.normalize_publication_result(push=value, pr=value)
    assert r["publicationState"]


def test_issue_delivery_prefers_the_publication_commit_then_workspace_head():
    d = issue_to_pr.normalize_issue_delivery(
        child={"deliveryOutcome": "passed"},
        publication_commit={"commit": "abc", "rejectedPaths": []},
        workspace_state={"head": "irrelevant"}, fallback_commit="fallback")
    assert d["commit"] == "abc"

    d2 = issue_to_pr.normalize_issue_delivery(
        child={"deliveryOutcome": "passed"}, publication_commit={"commit": "  "},
        workspace_state={"head": "ws-head"}, fallback_commit="fallback")
    assert d2["commit"] == "ws-head"

    d3 = issue_to_pr.normalize_issue_delivery(
        child={}, publication_commit={}, workspace_state={}, fallback_commit="fallback")
    assert d3["commit"] == "fallback"


def test_issue_delivery_downgrades_passed_when_the_commit_step_rejected_paths():
    d = issue_to_pr.normalize_issue_delivery(
        child={"deliveryOutcome": "passed"},
        publication_commit={"commit": "abc", "rejectedPaths": ["a.py"]},
        workspace_state={}, fallback_commit="fallback")
    assert d["state"] == "incomplete_delivery"
    assert "a.py" in d["rejectedPaths"]


def test_issue_delivery_falls_back_to_verification_state_when_outcome_is_unrecognized():
    d = issue_to_pr.normalize_issue_delivery(
        child={"verificationState": "merge_blocked"}, publication_commit={},
        workspace_state={}, fallback_commit="")
    assert d["state"] == "merge_blocked"


def test_verified_delivery_only_upgrades_a_delivery_that_was_already_passed():
    passed = issue_to_pr.normalize_verified_delivery(
        delivery={"state": "passed", "commit": "old"},
        verification={"verificationState": "passed", "candidateCommit": "new", "attempts": 1})
    assert passed["state"] == "passed"
    assert passed["commit"] == "new"

    already_bad = issue_to_pr.normalize_verified_delivery(
        delivery={"state": "incomplete_delivery", "commit": "old"},
        verification={"verificationState": "passed", "candidateCommit": "new"})
    assert already_bad["state"] == "incomplete_delivery"


def test_verified_delivery_collects_failing_test_commands():
    verified = issue_to_pr.normalize_verified_delivery(
        delivery={"state": "passed"},
        verification={"verificationState": "failed",
                      "verification": {"commands": [{"argv": ["pytest"], "exitCode": 1, "output": "boom"}]}})
    assert verified["state"] == "incomplete_delivery"
    assert verified["commandFailures"][0]["command"] == ["pytest"]


def test_publication_result_prefers_the_pr_state_over_the_push_state():
    assert issue_to_pr.normalize_publication_result(
        push={"publicationState": "pushed"}, pr={"publicationState": "published"}
    )["publicationState"] == "published"
    assert issue_to_pr.normalize_publication_result(
        push={"publicationState": "pushed"}, pr={})["publicationState"] == "pushed"
    assert issue_to_pr.normalize_publication_result(
        push={}, pr={})["publicationState"] == "publication_unavailable"
