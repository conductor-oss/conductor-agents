"""Conductor tasks for code_parallel's plan/merge/delivery/usage decisions.

This module exists so those decisions are reachable by ordinary unit tests
(see `common/code_parallel.py`) and so a malformed upstream value degrades a
result instead of failing the whole workflow -- neither was true while this
logic lived in JSON_JQ_TRANSFORM expressions.
"""

from __future__ import annotations

from conductor.client.worker.worker_task import worker_task

from common import code_parallel
from common.results import ok


@worker_task(task_definition_name="code_parallel_build_forks")
def code_parallel_build_forks(task):
    i = task.input_data or {}
    out = code_parallel.build_forks(
        repo_path=i.get("repoPath"), subtasks=i.get("subtasks"), change_dir=i.get("changeDir"),
        code_model=i.get("codeModel"), code_prompt_template=i.get("codePromptTemplate"),
        code_prompt_template_source=i.get("codePromptTemplateSource"),
        spec_context_path=i.get("specContextPath"), context_paths=i.get("contextPaths"),
        max_turns=i.get("maxTurns"), max_budget_usd=i.get("maxBudgetUsd"),
        model_profile=i.get("modelProfile"), model_policy=i.get("modelPolicy"),
        model_policy_source=i.get("modelPolicySource"), model_policy_sha256=i.get("modelPolicySha256"),
        models_config=i.get("modelsConfig"), model_overrides=i.get("modelOverrides"))
    return ok(task, out, [f"[code_parallel_build_forks] {len(out['dynamicTasks'])} subtask(s)"])


@worker_task(task_definition_name="code_parallel_merge_candidate")
def code_parallel_merge_candidate(task):
    i = task.input_data or {}
    out = code_parallel.select_merge_candidate(merge=i.get("merge"),
                                               fallback_commit=i.get("fallbackCommit"))
    return ok(task, out, [f"[code_parallel_merge_candidate] state={out['mergeState']}"])


@worker_task(task_definition_name="code_parallel_delivery_summary")
def code_parallel_delivery_summary(task):
    i = task.input_data or {}
    out = code_parallel.summarize_delivery(i.get("joined"))
    return ok(task, out, [f"[code_parallel_delivery_summary] state={out['state']} "
                          f"reports={len(out['reports'])}"])


@worker_task(task_definition_name="code_parallel_verification_outcome")
def code_parallel_verification_outcome(task):
    i = task.input_data or {}
    out = code_parallel.resolve_verification_outcome(
        candidate_commit=i.get("candidateCommit"), delivery=i.get("delivery"),
        issues=i.get("issues"), merge_state=i.get("mergeState"), plan_valid=i.get("planValid"),
        tested=i.get("tested"), test_state=i.get("testState"), tests_passed=i.get("testsPassed"))
    return ok(task, out, [f"[code_parallel_verification_outcome] state={out['state']} "
                          f"passed={out['passed']}"])


@worker_task(task_definition_name="code_parallel_handoff_summary")
def code_parallel_handoff_summary(task):
    i = task.input_data or {}
    out = code_parallel.fold_handoff_presented(verification=i.get("verification"),
                                               matched=i.get("matched"))
    return ok(task, out, [f"[code_parallel_handoff_summary] presented={out['presented']}"])


@worker_task(task_definition_name="code_parallel_aggregate_usage")
def code_parallel_aggregate_usage(task):
    i = task.input_data or {}
    out = code_parallel.aggregate_usage(
        joined=i.get("joined"), plan_cost=i.get("planCost"), plan_tokens=i.get("planTokens"),
        merge_cost=i.get("mergeCost"), merge_tokens=i.get("mergeTokens"),
        verify_cost=i.get("verifyCost") or 0, verify_tokens=i.get("verifyTokens") or 0)
    return ok(task, out, [f"[code_parallel_aggregate_usage] subtasks={out['subtaskCount']} "
                          f"totalCostUsd={out['totalCostUsd']}"])


@worker_task(task_definition_name="code_parallel_verification_report")
def code_parallel_verification_report(task):
    i = task.input_data or {}
    text = code_parallel.render_verification_report(
        branch=i.get("branch"), commit=i.get("commit"), issues=i.get("issues"),
        merged=i.get("merged"), outcome=i.get("outcome"))
    return ok(task, {"report": text}, ["[code_parallel_verification_report] composed"])


@worker_task(task_definition_name="usage_accumulate")
def usage_accumulate(task):
    """Add one new (tokens, cost) pair to a running total.

    Generic on purpose: several workflows accumulate agent usage across a
    repair or planning loop, and a throwing `def num: ...` jq coercion used to
    be duplicated at every one of those call sites.
    """
    i = task.input_data or {}
    def num(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    out = {"tokens": num(i.get("prevTokens")) + num(i.get("newTokens")),
          "cost": num(i.get("prevCost")) + num(i.get("newCost"))}
    return ok(task, out, [f"[usage_accumulate] tokens={out['tokens']} cost={out['cost']}"])
