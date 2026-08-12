"""Pure decision logic for openspec_generate_artifact."""

from __future__ import annotations


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _blank(value: object) -> bool:
    return not _text(value).strip()


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def build_prompt(*, instr: object, goal: object, feedback: object) -> str:
    """Compose the prompt asking the agent to write exactly one OpenSpec artifact."""
    instr = _mapping(instr)
    artifact = instr.get("artifactId")
    rules = _listed(instr.get("rules"))
    rules_text = "\n".join(f"- {rule}" for rule in rules)
    feedback_text = feedback if isinstance(feedback, str) and not _blank(feedback) else ""
    return (
        f"You are contributing the '{artifact}' artifact to an OpenSpec change that plans "
        f"this work:\n{goal}\n\n{instr.get('instruction')}"
        + (f"\n\nAdditional rules:\n{rules_text}" if rules else "")
        + f"\n\nWrite exactly this file: {instr.get('resolvedOutputPath')}\n\n"
        f"Template structure to follow (fill in every section):\n{instr.get('template')}"
        + (f"\n\nPrevious review feedback — address every item:\n{feedback_text}" if feedback_text else "")
        + "\n\nWrite only this OpenSpec artifact file; do not write application/source code."
    )
