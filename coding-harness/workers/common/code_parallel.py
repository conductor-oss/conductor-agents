"""Pure decision logic for code_parallel, replacing several JSON_JQ_TRANSFORM tasks.

Each function is total: it accepts whatever a degraded upstream task returned
(a null, a wrong type, a missing key) and always produces a well-formed
result, for the same reason as `common/test_plan.py` -- a throwing jq
expression ends the whole workflow FAILED, and none of this logic was
reachable by an ordinary unit test while it lived in jq.
"""

from __future__ import annotations

import json
from typing import Any

CODE_SUBTASK_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(python *)", "Bash(python3 *)", "Bash(node *)", "Bash(npm *)", "Bash(npx *)",
    "Bash(go *)", "Bash(cargo *)",
    "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git mv *)", "Bash(git rm *)",
    "Bash(mv *)", "Bash(rm *)", "Bash(mkdir *)", "Bash(cp *)", "Bash(touch *)",
]

_DELIVERY_PASSTHROUGH_STATES = {"incomplete_delivery", "audit_unavailable", "implementation_unavailable"}


def _num(value: object) -> float | int:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _blank(value: object) -> bool:
    return not _text(value).strip()


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _flatten_join(joined: object) -> list[dict]:
    """Flatten a FORK_JOIN_DYNAMIC's `{ref: {output: {...}}}` map to reports."""
    reports = []
    for value in _mapping(joined).values():
        entry = _mapping(value)
        output = _mapping(entry.get("output"))
        reports.append(output or entry)
    return reports


def build_forks(*, repo_path: object, subtasks: object, change_dir: object,
                code_model: object, code_prompt_template: object,
                code_prompt_template_source: object, spec_context_path: object,
                context_paths: object, max_turns: object, max_budget_usd: object,
                model_profile: object, model_policy: object, model_policy_source: object,
                model_policy_sha256: object, models_config: object,
                model_overrides: object) -> dict:
    """Build one dynamic-fork SUB_WORKFLOW descriptor per validated plan entry."""
    dynamic_tasks: list[dict] = []
    dynamic_inputs: dict[str, dict] = {}
    group_ids: list[str] = []
    all_files: list[str] = []
    for subtask in _listed(subtasks):
        entry = _mapping(subtask)
        subtask_id = _text(entry.get("id"))
        if not subtask_id:
            continue
        files = [path for path in _listed(entry.get("files")) if isinstance(path, str) and path]
        description = f"{_text(entry.get('description'))}\nTarget files: {', '.join(files)}"
        group_ids.append(subtask_id)
        all_files.extend(files)
        dynamic_tasks.append({
            "name": "code_subtask", "taskReferenceName": subtask_id, "type": "SUB_WORKFLOW",
            "optional": True, "subWorkflowParam": {"name": "code_subtask", "version": 1},
        })
        dynamic_inputs[subtask_id] = {
            "repoPath": repo_path, "name": subtask_id, "model": code_model,
            "maxTurns": max_turns, "maxBudgetUsd": max_budget_usd,
            "prompt": f"{description}\n\nPlanning context: {change_dir}. Read the supplied "
                      "design material and only edit your assigned files.",
            "promptTemplate": code_prompt_template,
            "promptTemplateSource": code_prompt_template_source,
            "templateKey": "code", "promptContext": {"subtask": description},
            "specContextPath": spec_context_path, "contextPaths": context_paths,
            "allowedTools": CODE_SUBTASK_TOOLS, "allowedWriteRoots": files,
            "modelProfile": model_profile, "modelPolicy": model_policy,
            "modelPolicySource": model_policy_source, "modelPolicySha256": model_policy_sha256,
            "modelsConfig": models_config, "modelOverrides": model_overrides,
        }
    return {"dynamicTasks": dynamic_tasks, "dynamicTasksInput": dynamic_inputs,
            "groupIds": ",".join(group_ids), "allowedWriteRoots": sorted(set(all_files))}


def select_merge_candidate(*, merge: object, fallback_commit: object) -> dict:
    """Pick the merged commit, or fall back to the pre-merge head."""
    merge_result = _mapping(_mapping(merge).get("merge"))
    commit = merge_result.get("commit")
    resolved = commit if isinstance(commit, str) and not _blank(commit) else fallback_commit
    merge_state = _mapping(merge).get("mergeState")
    return {"commit": resolved,
            "mergeState": merge_state if isinstance(merge_state, str) else "merge_blocked"}


def summarize_delivery(joined: object) -> dict:
    """Roll every subtask's delivery report into one aggregate state."""
    normalized = []
    for report in _flatten_join(joined):
        state = report.get("deliveryOutcome")
        if not isinstance(state, str):
            status = report.get("status")
            state = status if isinstance(status, str) else "implementation_unavailable"
        attempts = report.get("repairAttempts")
        normalized.append({
            "state": state,
            "missingPaths": _listed(report.get("missingPaths")),
            "rejectedPaths": _listed(report.get("rejectedPaths")),
            "commandFailures": _listed(report.get("commandFailures")),
            "sandboxDenials": _listed(report.get("sandboxDenials")),
            "repairAttempts": _num(attempts),
            "commit": report.get("commit") or "",
        })
    states = [entry["state"] for entry in normalized]
    if not normalized:
        state = "implementation_unavailable"
    elif all(value == "passed" for value in states):
        state = "passed"
    elif any(value == "implementation_unavailable" for value in states):
        state = "implementation_unavailable"
    elif any(value == "audit_unavailable" for value in states):
        state = "audit_unavailable"
    else:
        state = "incomplete_delivery"

    def unique_across(key: str) -> list:
        seen: list = []
        for entry in normalized:
            for path in entry[key]:
                if path not in seen:
                    seen.append(path)
        return seen

    return {
        "state": state, "reports": normalized,
        "missingPaths": unique_across("missingPaths"),
        "rejectedPaths": unique_across("rejectedPaths"),
        "commandFailures": [failure for entry in normalized for failure in entry["commandFailures"]],
        "sandboxDenials": unique_across("sandboxDenials"),
        "repairAttempts": sum(entry["repairAttempts"] for entry in normalized),
    }


def resolve_verification_outcome(*, candidate_commit: object, delivery: object, issues: object,
                                  merge_state: object, plan_valid: object, tested: object,
                                  test_state: object, tests_passed: object) -> dict:
    """Fold plan validity, the merge, delivery, and the test cycle into one verdict."""
    delivery_state = _mapping(delivery).get("state")
    passed = plan_valid is True and merge_state == "merged" and delivery_state == "passed"
    if passed:
        state = "passed"
    elif merge_state != "merged":
        state = "merge_blocked"
    elif delivery_state in _DELIVERY_PASSTHROUGH_STATES:
        state = delivery_state
    else:
        state = "implementation_unavailable"
    final_commit = tested if isinstance(tested, str) and not _blank(tested) else candidate_commit
    return {
        "passed": passed, "testsPassed": tests_passed is True,
        "testCycleState": test_state if isinstance(test_state, str) else "not_run",
        "candidateCommit": final_commit, "state": state, "executionOutcome": state,
        "mergeState": merge_state, "planValid": plan_valid is True,
        "delivery": delivery, "issues": _listed(issues),
    }


def fold_handoff_presented(*, verification: object, matched: object) -> dict:
    """Add whether the verified candidate is exactly what got presented."""
    result = dict(_mapping(verification))
    result["presented"] = matched is True and result.get("passed") is True
    return result


def aggregate_usage(*, joined: object, plan_cost: object, plan_tokens: object,
                    merge_cost: object, merge_tokens: object,
                    verify_cost: object = 0, verify_tokens: object = 0) -> dict:
    """Sum token and dollar usage across the plan, every subtask, merge, and tests."""
    per_subtask = [{
        "status": report.get("status") if isinstance(report.get("status"), str) else "unknown",
        "filesChanged": _listed(report.get("filesChanged")),
        "costUsd": _num(report.get("costUsd")), "tokenUsed": _num(report.get("tokenUsed")),
    } for report in _flatten_join(joined)]
    subtask_tokens = sum(entry["tokenUsed"] for entry in per_subtask)
    subtask_cost = sum(entry["costUsd"] for entry in per_subtask)
    plan_tokens, plan_cost = _num(plan_tokens), _num(plan_cost)
    merge_tokens, merge_cost = _num(merge_tokens), _num(merge_cost)
    verify_tokens, verify_cost = _num(verify_tokens), _num(verify_cost)
    return {
        "perSubtask": per_subtask, "subtaskCount": len(per_subtask),
        "tokens": {"plan": plan_tokens, "subtasks": subtask_tokens,
                   "merge": merge_tokens, "verify": verify_tokens},
        "cost": {"plan": plan_cost, "subtasks": subtask_cost,
                 "merge": merge_cost, "verify": verify_cost},
        "totalTokens": plan_tokens + subtask_tokens + merge_tokens + verify_tokens,
        "totalCostUsd": plan_cost + subtask_cost + merge_cost + verify_cost,
    }


def _issue_text(issue: object) -> str:
    if isinstance(issue, dict):
        message = issue.get("message")
        return message if isinstance(message, str) else json.dumps(issue, sort_keys=True)
    return str(issue)


def render_verification_report(*, branch: object, commit: object, issues: object,
                               merged: object, outcome: object) -> str:
    """Compose the human-readable verification summary."""
    outcome = _mapping(outcome)
    lines = "\n".join(f"- {_issue_text(issue)}" for issue in _listed(issues))
    plan_state = "valid" if outcome.get("planValid") else "rejected"
    return (
        f"Branch: {branch or '(none)'} @ {commit or '(no commit)'}\n"
        f"Plan: {plan_state}\n"
        f"Merge: {len(_listed(merged))} branch(es) merged\n"
        f"Outcome: {outcome.get('executionOutcome') or 'unknown'}\n\n"
        f"Plan issues:\n{lines or '  none'}"
    )
