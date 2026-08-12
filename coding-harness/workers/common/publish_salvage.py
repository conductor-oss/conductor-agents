"""Pure decision logic for publish_salvage, replacing three JSON_JQ_TRANSFORM tasks."""

from __future__ import annotations

import re


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _blank(value: object) -> bool:
    return not _text(value).strip()


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def build_salvage_plan(all_input: object) -> dict:
    """Recover a publishable branch/repo/commit from a failed run's own state.

    Prefers whatever the failed workflow's own variables recorded; falls back
    to the workspace/clone task's output when the variables never got that far.
    """
    workflow = _mapping(all_input)
    failed = _mapping(workflow.get("failedWorkflow"))
    variables = _mapping(failed.get("variables"))
    inputs = _mapping(failed.get("input"))
    tasks = _listed(failed.get("tasks"))

    workspace_output: dict = {}
    for task in tasks:
        task = _mapping(task)
        if task.get("referenceTaskName") in {"workspace", "clone"}:
            workspace_output = _mapping(task.get("outputData"))
            break

    def prefer(variable_value: object, workspace_value: object) -> str:
        text = _text(variable_value)
        if not _blank(text):
            return text
        return _text(workspace_value)

    branch = prefer(variables.get("currentBranch"), workspace_output.get("branch"))
    repo = prefer(variables.get("currentWorkspacePath"),
                  workspace_output.get("repoPath") or workspace_output.get("worktreePath"))
    commit = prefer(variables.get("candidateCommit"), workspace_output.get("baseCommit"))
    return {
        "branch": branch, "repoPath": repo, "commit": commit,
        "issueNumber": str(inputs.get("issueNumber") or ""),
        "base": str(inputs.get("base") or "main"),
        # "true"/"false" stay strings: salvage_gate is a value-param SWITCH.
        "canPublish": "true" if branch and repo else "false",
        "reason": workflow.get("reason") or "the run failed before reaching its publication gate",
        "workflowId": workflow.get("workflowId") or "",
    }


def compose_salvage_body(plan: object) -> str:
    """Compose the one-section PR body explaining why this is a salvage."""
    reason = _mapping(plan).get("reason") or "the run ended before delivery completed"
    reason = str(reason)
    reason = re.sub(r"(?m)^#{1,6}\s+", "", reason)
    reason = re.sub(r"\s+", " ", reason).strip()
    return f"## Summary\n\n{reason[:480]}"


def resolve_salvage_outcome(number: object) -> dict:
    """Name whether the salvage produced a PR or only a pushed branch."""
    has_pr = isinstance(number, (int, float)) and not isinstance(number, bool) and number > 0
    return {"state": "salvaged" if has_pr else "pushed_no_pr"}
