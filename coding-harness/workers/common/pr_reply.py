"""Compose the human-readable PR reply address_pr posts after a repair pass.

Both call sites (the initial code_parallel pass and a human-requested
revision) format the same subtask list and findings list; only the framing
prose around them differs.
"""

from __future__ import annotations


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def _text(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _format_subtasks(subtasks: object) -> str:
    lines = []
    for entry in _listed(subtasks):
        entry = entry if isinstance(entry, dict) else {}
        lines.append(f"- **{_text(entry.get('id'), '?')}**: {_text(entry.get('description'))}")
    return "\n".join(lines) if lines else "_none_"


def _format_findings(findings: object) -> str:
    lines = [f"- {finding}" for finding in _listed(findings)]
    return "\n".join(lines) if lines else "_none_"


def compose_parallel_reply(*, pr_number: object, head: object, proposal_text: object,
                           subtasks: object, findings: object, verified: object) -> str:
    """The reply posted after code_parallel addresses PR review feedback."""
    return (
        f"Addressed the review feedback on PR #{pr_number}.\n\n"
        f"## Summary\n{_text(proposal_text, '(no proposal text available)')}\n\n"
        f"## Subtasks\n{_format_subtasks(subtasks)}\n\n"
        f"## Verification\n- Verified: {str(verified is True).lower()}\n"
        f"- Findings:\n{_format_findings(findings)}\n\n"
        f"---\n_Engine: `code_parallel`, branch `{_text(head)}`._"
    )


def compose_revision_reply(*, proposal_text: object, subtasks: object, findings: object) -> str:
    """The reply posted after a human-requested approval revision."""
    return (
        "Addressed the PR feedback and the requested approval revision.\n\n"
        f"## Summary\n{_text(proposal_text, '(no proposal text available)')}\n\n"
        f"## Subtasks\n{_format_subtasks(subtasks)}\n\n"
        f"## Verification findings\n{_format_findings(findings)}"
    )
