"""Workflow-status lookups for housekeeping that spans runs.

The only consumer is ``git.prune_finished_runs`` (via ``workspace_prepare``): a run worktree is
removed only when the server says its workflow is no longer running. Any failure to ask — no server,
an id that is not a workflow — answers ``None`` and the worktree is kept.
"""
from __future__ import annotations

RUNNING_STATES = {"RUNNING", "PAUSED"}


def workflow_running(workflow_id: str) -> bool | None:
    """True/False when the Conductor server answers for ``workflow_id``; None when it cannot be asked."""
    try:
        from conductor.client.configuration.configuration import Configuration
        from conductor.client.orkes.orkes_workflow_client import OrkesWorkflowClient
        wf = OrkesWorkflowClient(Configuration()).get_workflow(str(workflow_id), include_tasks=False)
    except Exception:  # noqa: BLE001 -- unreachable server or unknown id: keep the worktree
        return None
    return str(getattr(wf, "status", "") or "").upper() in RUNNING_STATES
