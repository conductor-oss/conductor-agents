"""Which decision contract a WAIT review gate accepts.

A gate reports the workflow that *called* it (``callerWorkflow``), but the payload
it accepts belongs to the sub-workflow that owns it.  Both the approval modal and
the chat ``decide_approval`` tool must speak the owner's contract, or a decision
is normalised to ``unknown`` and the gate re-opens (or, for campaign checkpoints,
falls into the revision branch) instead of doing what the reviewer asked.
"""

from __future__ import annotations


def gate_contract(gate_workflow: str, draft: dict) -> str:
    """Return the contract name for a gate: its reporting workflow, with one exception.

    feature_campaign's PR-draft checkpoint is pr_draft_approval's ``pr_gate``: the
    same ``pr_decision`` worker as issue_to_pr reads it, so it wants
    ``approve``/``revise``/``stop`` plus the PR title and body -- not the campaign
    checkpoint's ``continue``/``adopt_edits``.  The workflow stamps ``kind: pr_draft``
    on the draft itself; callers may also copy the gate's ``phase`` onto it.
    Every other feature_campaign checkpoint (design, plan, wave, final) keeps the
    campaign contract: ``continue``/``revise``/``adopt_edits``/``stop``.
    """
    if gate_workflow == "feature_campaign" and "pr_draft" in (draft.get("kind"), draft.get("phase")):
        return "issue_to_pr"
    return gate_workflow
