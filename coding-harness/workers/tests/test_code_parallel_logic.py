"""Unit tests for the logic that used to live in code_parallel's jq expressions.

As with `common/test_plan.py`, the property that matters most is totality: a
degraded upstream task (a null, a wrong type, a missing key) must still
produce a well-formed result, because a throwing expression used to fail the
whole workflow.
"""

from __future__ import annotations

import pytest

from common import code_parallel

JUNK = (None, "", "  ", 0, -1, True, False, [], {}, [None], "a string",
        {"nested": {"deep": 1}}, [{"argv": None}])


@pytest.mark.parametrize("value", JUNK)
def test_build_forks_never_raises(value):
    result = code_parallel.build_forks(
        repo_path=value, subtasks=value, change_dir=value, code_model=value,
        code_prompt_template=value, code_prompt_template_source=value,
        spec_context_path=value, context_paths=value, max_turns=value,
        max_budget_usd=value, model_profile=value, model_policy=value,
        model_policy_source=value, model_policy_sha256=value, models_config=value,
        model_overrides=value)
    assert result["dynamicTasks"] == []
    assert result["dynamicTasksInput"] == {}
    assert result["groupIds"] == ""
    assert result["allowedWriteRoots"] == []


@pytest.mark.parametrize("value", JUNK)
def test_select_merge_candidate_never_raises(value):
    result = code_parallel.select_merge_candidate(merge=value, fallback_commit=value)
    assert "commit" in result and "mergeState" in result


@pytest.mark.parametrize("value", JUNK)
def test_summarize_delivery_never_raises(value):
    result = code_parallel.summarize_delivery(value)
    assert result["state"]
    assert isinstance(result["reports"], list)


@pytest.mark.parametrize("value", JUNK)
def test_resolve_verification_outcome_never_raises(value):
    result = code_parallel.resolve_verification_outcome(
        candidate_commit=value, delivery=value, issues=value, merge_state=value,
        plan_valid=value, tested=value, test_state=value, tests_passed=value)
    assert result["state"]


@pytest.mark.parametrize("value", JUNK)
def test_fold_handoff_and_aggregate_never_raise(value):
    assert "presented" in code_parallel.fold_handoff_presented(verification=value, matched=value)
    usage = code_parallel.aggregate_usage(joined=value, plan_cost=value, plan_tokens=value,
                                          merge_cost=value, merge_tokens=value)
    assert isinstance(usage["totalCostUsd"], (int, float))


@pytest.mark.parametrize("value", JUNK)
def test_render_verification_report_never_raises(value):
    text = code_parallel.render_verification_report(
        branch=value, commit=value, issues=value, merged=value, outcome=value)
    assert isinstance(text, str) and text


def test_build_forks_assigns_one_dynamic_subtask_per_plan_entry():
    result = code_parallel.build_forks(
        repo_path="/repo", subtasks=[
            {"id": "api", "description": "Implement API", "files": ["src/api.py"]},
            {"id": "cli", "description": "Implement CLI", "files": ["src/cli.py", "src/cli2.py"]},
        ],
        change_dir="design context", code_model="m", code_prompt_template="",
        code_prompt_template_source="", spec_context_path="", context_paths=[],
        max_turns=4, max_budget_usd=1, model_profile="p", model_policy={},
        model_policy_source="", model_policy_sha256="", models_config="", model_overrides={})

    assert [task["taskReferenceName"] for task in result["dynamicTasks"]] == ["api", "cli"]
    assert result["groupIds"] == "api,cli"
    assert result["allowedWriteRoots"] == ["src/api.py", "src/cli.py", "src/cli2.py"]
    assert result["dynamicTasksInput"]["cli"]["allowedWriteRoots"] == ["src/cli.py", "src/cli2.py"]
    assert "Target files: src/api.py" in result["dynamicTasksInput"]["api"]["prompt"]


def test_build_forks_skips_a_subtask_with_no_id():
    result = code_parallel.build_forks(
        repo_path="/repo", subtasks=[{"description": "no id", "files": ["a.py"]}],
        change_dir="", code_model="", code_prompt_template="", code_prompt_template_source="",
        spec_context_path="", context_paths=[], max_turns=1, max_budget_usd=1,
        model_profile="", model_policy={}, model_policy_source="", model_policy_sha256="",
        models_config="", model_overrides={})
    assert result["dynamicTasks"] == []
    assert result["groupIds"] == ""


def test_merge_candidate_falls_back_when_the_merge_produced_no_commit():
    assert code_parallel.select_merge_candidate(
        merge={"merge": {"commit": "abc"}, "mergeState": "merged"},
        fallback_commit="original")["commit"] == "abc"
    assert code_parallel.select_merge_candidate(
        merge={"merge": {"commit": "  "}}, fallback_commit="original")["commit"] == "original"
    assert code_parallel.select_merge_candidate(
        merge={}, fallback_commit="original") == {"commit": "original", "mergeState": "merge_blocked"}


def test_delivery_summary_flattens_a_dynamic_fork_join():
    joined = {
        "api": {"output": {"deliveryOutcome": "passed", "repairAttempts": 1}},
        "cli": {"output": {"status": "incomplete_delivery", "missingPaths": ["cli.py"]}},
    }
    result = code_parallel.summarize_delivery(joined)
    assert result["state"] == "incomplete_delivery"
    assert result["missingPaths"] == ["cli.py"]
    assert result["repairAttempts"] == 1


def test_delivery_summary_passes_only_when_every_subtask_passed():
    joined = {"a": {"output": {"deliveryOutcome": "passed"}},
             "b": {"output": {"deliveryOutcome": "passed"}}}
    assert code_parallel.summarize_delivery(joined)["state"] == "passed"


def test_delivery_summary_with_no_subtasks_is_unavailable_not_passed():
    assert code_parallel.summarize_delivery({})["state"] == "implementation_unavailable"


def test_verification_outcome_prefers_the_tested_commit_when_present():
    outcome = code_parallel.resolve_verification_outcome(
        candidate_commit="merged-sha", delivery={"state": "passed"}, issues=[],
        merge_state="merged", plan_valid=True, tested="repaired-sha",
        test_state="tests_passed", tests_passed=True)
    assert outcome["candidateCommit"] == "repaired-sha"
    assert outcome["passed"] is True


def test_verification_outcome_falls_back_to_candidate_when_untested():
    outcome = code_parallel.resolve_verification_outcome(
        candidate_commit="merged-sha", delivery={"state": "passed"}, issues=[],
        merge_state="merged", plan_valid=True, tested="  ",
        test_state=None, tests_passed=None)
    assert outcome["candidateCommit"] == "merged-sha"
    assert outcome["testCycleState"] == "not_run"


def test_handoff_presented_requires_both_a_match_and_a_pass():
    assert code_parallel.fold_handoff_presented(
        verification={"passed": True}, matched=True)["presented"] is True
    assert code_parallel.fold_handoff_presented(
        verification={"passed": True}, matched=False)["presented"] is False
    assert code_parallel.fold_handoff_presented(
        verification={"passed": False}, matched=True)["presented"] is False


def test_aggregate_usage_sums_plan_subtasks_merge_and_verify():
    joined = {"a": {"output": {"costUsd": 1.5, "tokenUsed": 100}},
             "b": {"output": {"costUsd": 2.5, "tokenUsed": 200}}}
    usage = code_parallel.aggregate_usage(joined=joined, plan_cost=1, plan_tokens=10,
                                          merge_cost=0.5, merge_tokens=5,
                                          verify_cost=0.25, verify_tokens=3)
    assert usage["subtaskCount"] == 2
    assert usage["tokens"]["subtasks"] == 300
    assert usage["totalCostUsd"] == pytest.approx(1 + 4.0 + 0.5 + 0.25)
    assert usage["totalTokens"] == 10 + 300 + 5 + 3


def test_verification_report_names_no_issues_as_none():
    text = code_parallel.render_verification_report(
        branch="main", commit="abc123", issues=[], merged=["a", "b"],
        outcome={"planValid": True, "executionOutcome": "passed"})
    assert "Merge: 2 branch(es) merged" in text
    assert "Plan: valid" in text
    assert "none" in text


def test_verification_report_lists_issue_messages():
    text = code_parallel.render_verification_report(
        branch="main", commit="abc", issues=[{"message": "overlap"}, "raw string"],
        merged=[], outcome={"planValid": False, "executionOutcome": "plan_rejected"})
    assert "- overlap" in text
    assert "- raw string" in text
    assert "Plan: rejected" in text
