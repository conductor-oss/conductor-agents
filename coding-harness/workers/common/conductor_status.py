"""Workflow-status lookups for housekeeping that spans runs.

The only consumer is ``git.prune_finished_runs`` (via ``workspace_prepare``): a run worktree is
removed only when the server says its workflow is over for good. A failed, stopped or timed-out run
keeps its checkout: Conductor's retry resumes it in that very directory, and removing it made every
resumed step fail on a path that no longer existed. Any failure to ask — no server, an id that is not
a workflow — answers ``None`` and the worktree is kept.
"""
from __future__ import annotations

RUNNING_STATES = {"RUNNING", "PAUSED"}
PRUNABLE_STATES = {"COMPLETED"}


def workflow_status(workflow_id: str) -> str | None:
    """The run's status when the Conductor server answers for ``workflow_id``; None when it cannot be asked."""
    try:
        from conductor.client.configuration.configuration import Configuration
        from conductor.client.orkes.orkes_workflow_client import OrkesWorkflowClient
        wf = OrkesWorkflowClient(Configuration()).get_workflow(str(workflow_id), include_tasks=False)
    except Exception:  # noqa: BLE001 -- unreachable server or unknown id: keep the worktree
        return None
    return str(getattr(wf, "status", "") or "").upper() or None


def workflow_running(workflow_id: str) -> bool | None:
    status = workflow_status(workflow_id)
    return None if status is None else status in RUNNING_STATES


def workflow_prunable(workflow_id: str) -> bool | None:
    """True only for a run that finished and can never resume; a retryable run keeps its checkout."""
    status = workflow_status(workflow_id)
    return None if status is None else status in PRUNABLE_STATES
