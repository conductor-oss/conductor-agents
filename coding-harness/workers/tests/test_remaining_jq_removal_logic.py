"""Unit tests for the pure logic that replaced the last 22 jq expressions.

One file covering several small modules, mirroring the depth of
test_code_parallel_logic.py and test_issue_to_pr_logic.py for the modules that
did not get their own dedicated file: gate_decision, pr_reply, pr_review,
publish_salvage, feature_campaign, openspec_artifact_drain, code_subtask,
openspec_development, and openspec_generate_artifact.
"""

from __future__ import annotations

import pytest

from common import (code_subtask, feature_campaign, gate_decision, openspec_artifact_drain,
                    openspec_development, openspec_generate_artifact, pr_reply, pr_review,
                    publish_salvage)

JUNK = (None, "", "  ", 0, True, [], {}, "a string", [None], {"nested": 1})


# --- gate_decision -------------------------------------------------------------

@pytest.mark.parametrize("value", JUNK)
def test_gate_decision_never_raises(value):
    result = gate_decision.resolve_gate_decision(value, can_investigate=bool(value))
    assert result["action"] in {"approve", "investigate", "revise", "stop", "unknown"}
    assert gate_decision.summarize_review(value)["verdict"] in {"approve", "request_changes"}


def test_gate_decision_requires_approved_flag_alongside_the_approve_action():
    assert gate_decision.resolve_gate_decision(
        {"action": "approve", "approved": True})["action"] == "approve"
    assert gate_decision.resolve_gate_decision(
        {"action": "approve", "approved": False})["action"] == "unknown"


def test_gate_decision_treats_blank_feedback_as_no_request():
    assert gate_decision.resolve_gate_decision({"action": "revise", "feedback": "  "})["action"] == "unknown"
    assert gate_decision.resolve_gate_decision({"action": "revise", "feedback": "fix x"})["action"] == "revise"


def test_gate_decision_investigate_requires_budget_and_feedback():
    assert gate_decision.resolve_gate_decision(
        {"action": "investigate", "feedback": "why?"}, can_investigate=False)["action"] == "unknown"
    assert gate_decision.resolve_gate_decision(
        {"action": "investigate", "feedback": "why?"}, can_investigate=True)["action"] == "investigate"


def test_gate_decision_infers_approve_from_the_approved_flag_alone():
    assert gate_decision.resolve_gate_decision({"approved": True})["action"] == "approve"
    assert gate_decision.resolve_gate_decision({"approved": False})["action"] == "unknown"


def test_summarize_review_is_clean_only_with_zero_blocking_comments():
    assert gate_decision.summarize_review({"comments": []})["verdict"] == "approve"
    assert gate_decision.summarize_review(
        {"comments": [{"severity": "blocking", "path": "a.py", "line": 1, "body": "fix"}]}
    )["verdict"] == "request_changes"
    # A comment missing any required field is not a blocking comment.
    assert gate_decision.summarize_review(
        {"comments": [{"severity": "blocking", "path": "a.py", "line": 1, "body": "  "}]}
    )["verdict"] == "approve"


# --- pr_reply --------------------------------------------------------------

@pytest.mark.parametrize("value", JUNK)
def test_pr_reply_composers_never_raise(value):
    assert isinstance(pr_reply.compose_parallel_reply(
        pr_number=value, head=value, proposal_text=value, subtasks=value,
        findings=value, verified=value), str)
    assert isinstance(pr_reply.compose_revision_reply(
        proposal_text=value, subtasks=value, findings=value), str)


def test_pr_reply_falls_back_when_subtasks_or_findings_are_empty():
    text = pr_reply.compose_parallel_reply(pr_number=1, head="h", proposal_text="p",
                                           subtasks=[], findings=[], verified=False)
    assert "_none_" in text


# --- pr_review ---------------------------------------------------------------

@pytest.mark.parametrize("value", JUNK)
def test_normalize_investigation_never_raises(value):
    result = pr_review.normalize_investigation(
        structured=value, status=value, error=value, session_id=value, prior_session_id=value,
        question=value, prior_review=value, history=value, prior_tokens=value, tokens=value,
        prior_cost=value, cost=value)
    assert "answer" in result and "review" in result


def test_investigation_keeps_the_prior_session_when_none_is_returned():
    result = pr_review.normalize_investigation(
        structured={"answer": "ok"}, status="success", error="", session_id=None,
        prior_session_id="s-1", question="q", prior_review={}, history=[],
        prior_tokens=0, tokens=1, prior_cost=0, cost=0.1)
    assert result["sessionId"] == "s-1"


# --- publish_salvage -----------------------------------------------------------

@pytest.mark.parametrize("value", JUNK)
def test_publish_salvage_never_raises(value):
    plan = publish_salvage.build_salvage_plan(value)
    assert plan["canPublish"] in {"true", "false"}
    assert isinstance(publish_salvage.compose_salvage_body(value), str)
    assert publish_salvage.resolve_salvage_outcome(value)["state"] in {"salvaged", "pushed_no_pr"}


def test_salvage_plan_prefers_variables_over_the_workspace_task_output():
    plan = publish_salvage.build_salvage_plan({
        "failedWorkflow": {
            "variables": {"currentBranch": "from-vars", "currentWorkspacePath": "/vars"},
            "input": {"issueNumber": 7, "base": "main"},
            "tasks": [{"referenceTaskName": "workspace",
                      "outputData": {"branch": "from-ws", "repoPath": "/ws"}}],
        }
    })
    assert plan["branch"] == "from-vars"
    assert plan["repoPath"] == "/vars"
    assert plan["canPublish"] == "true"


def test_salvage_plan_falls_back_to_the_workspace_task_when_variables_are_blank():
    plan = publish_salvage.build_salvage_plan({
        "failedWorkflow": {
            "variables": {},
            "tasks": [{"referenceTaskName": "clone",
                      "outputData": {"branch": "from-ws", "worktreePath": "/ws"}}],
        }
    })
    assert plan["branch"] == "from-ws"
    assert plan["repoPath"] == "/ws"


def test_salvage_body_caps_at_480_characters():
    body = publish_salvage.compose_salvage_body({"reason": "x" * 1000})
    assert len(body) <= 500  # heading + 480 chars of reason


# --- feature_campaign -----------------------------------------------------------

@pytest.mark.parametrize("value", JUNK)
def test_feature_campaign_never_raises(value):
    assert isinstance(feature_campaign.is_implementation_done(outcome=value, remaining=value), bool)
    plan = feature_campaign.resolve_campaign_publication_plan(
        outcome=value, requested_draft=value, test_state=value, tests_passed=value,
        tested=value, committed=value)
    assert "draft" in plan


def test_campaign_publication_plan_drafts_when_tests_did_not_pass():
    plan = feature_campaign.resolve_campaign_publication_plan(
        outcome="verified", requested_draft=False, test_state="tests_failed_after_fix_budget",
        tests_passed=False, tested="sha", committed="old-sha")
    assert plan["draft"] is True
    assert plan["head"] == "sha"


def test_campaign_publication_plan_uses_committed_sha_when_untested():
    plan = feature_campaign.resolve_campaign_publication_plan(
        outcome="verified", requested_draft=False, test_state=None, tests_passed=None,
        tested="  ", committed="old-sha")
    assert plan["head"] == "old-sha"


def test_campaign_publication_plan_forces_draft_when_an_agent_authored_test_was_used():
    plan = feature_campaign.resolve_campaign_publication_plan(
        outcome="verified", requested_draft=False, test_state="tests_passed",
        tests_passed=True, tested="sha", committed="old-sha", agent_authored_test=True)
    assert plan["draft"] is True
    assert plan["agentAuthoredTest"] is True
    assert plan["agentAuthoredTestNote"] != ""

    not_authored = feature_campaign.resolve_campaign_publication_plan(
        outcome="verified", requested_draft=False, test_state="tests_passed",
        tests_passed=True, tested="sha", committed="old-sha", agent_authored_test=False)
    assert not_authored["draft"] is False
    assert not_authored["agentAuthoredTest"] is False
    assert not_authored["agentAuthoredTestNote"] == ""


# --- openspec_artifact_drain -----------------------------------------------------

@pytest.mark.parametrize("value", JUNK)
def test_openspec_artifact_drain_never_raises(value):
    ready = openspec_artifact_drain.select_ready(
        status=value, generated=value, repo_path=value, change_name=value, feedback=value,
        goal=value, model=value, max_turns=value, max_budget_usd=value, model_profile=value,
        model_policy=value, model_policy_source=value, model_policy_sha256=value,
        models_config=value, model_overrides=value)
    assert ready["dynamicTasks"] == []
    progress = openspec_artifact_drain.merge_pass_progress(
        fan_output=value, ready_ids=value, prev_generated=value, prev_files=value,
        prev_cost=value, prev_tokens=value)
    assert isinstance(progress["generated"], list)


def test_merge_pass_progress_only_accumulates_when_something_was_ready():
    stale = openspec_artifact_drain.merge_pass_progress(
        fan_output={"a": {"output": {"costUsd": 5, "tokenUsed": 50}}},
        ready_ids=[], prev_generated=["a"], prev_files=["x.md"], prev_cost=1, prev_tokens=10)
    assert stale["costUsd"] == 1 and stale["tokenUsed"] == 10 and stale["filesChanged"] == ["x.md"]

    fresh = openspec_artifact_drain.merge_pass_progress(
        fan_output={"a": {"output": {"costUsd": 5, "tokenUsed": 50, "filesChanged": ["a.md"]}}},
        ready_ids=["a"], prev_generated=[], prev_files=[], prev_cost=1, prev_tokens=10)
    assert fresh["costUsd"] == 6 and fresh["tokenUsed"] == 60 and fresh["filesChanged"] == ["a.md"]
    assert fresh["generated"] == ["a"]


# --- code_subtask -----------------------------------------------------------

@pytest.mark.parametrize("value", JUNK)
def test_code_subtask_delivery_outcome_never_raises(value):
    result = code_subtask.resolve_delivery_outcome(
        planned_paths=value, commit=value, audit=value, agent=value,
        tested=value, test_state=value, tests_passed=value)
    assert result["state"]


def test_delivery_outcome_passes_only_when_every_planned_path_is_staged():
    passed = code_subtask.resolve_delivery_outcome(
        planned_paths=["a.py"], commit={"stagedPaths": ["a.py"], "rejectedPaths": []},
        audit={}, agent={}, tested=None, test_state=None, tests_passed=None)
    assert passed["state"] == "passed"

    missing = code_subtask.resolve_delivery_outcome(
        planned_paths=["a.py", "b.py"], commit={"stagedPaths": ["a.py"], "rejectedPaths": []},
        audit={}, agent={"agentCompleted": True}, tested=None, test_state=None, tests_passed=None)
    assert missing["state"] == "incomplete_delivery"
    assert missing["missingPaths"] == ["b.py"]


def test_delivery_outcome_is_audit_unavailable_before_implementation_unavailable():
    result = code_subtask.resolve_delivery_outcome(
        planned_paths=["a.py"], commit={}, audit={"auditAvailable": False}, agent={},
        tested=None, test_state=None, tests_passed=None)
    assert result["state"] == "audit_unavailable"


# --- openspec_development / openspec_generate_artifact --------------------------

@pytest.mark.parametrize("value", JUNK)
def test_openspec_modules_never_raise(value):
    usage = openspec_development.aggregate_usage(assessment=value, child=value, verification=value)
    assert isinstance(usage["totalCostUsd"], (int, float))
    prompt = openspec_generate_artifact.build_prompt(instr=value, goal=value, feedback=value)
    assert isinstance(prompt, str)


def test_openspec_usage_sums_all_three_sources():
    usage = openspec_development.aggregate_usage(
        assessment={"tokenUsed": 10, "costUsd": 1}, child={"totalTokens": 20, "totalCostUsd": 2},
        verification={"tokenUsed": 5, "costUsd": 0.5})
    assert usage["totalTokens"] == 35
    assert usage["totalCostUsd"] == 3.5


def test_build_prompt_omits_blank_feedback_and_empty_rules():
    prompt = openspec_generate_artifact.build_prompt(
        instr={"artifactId": "design", "instruction": "Write the design.",
              "template": "## Design", "resolvedOutputPath": "design.md", "rules": []},
        goal="Ship the thing", feedback="")
    assert "Additional rules" not in prompt
    assert "address every item" not in prompt
    assert "design.md" in prompt
