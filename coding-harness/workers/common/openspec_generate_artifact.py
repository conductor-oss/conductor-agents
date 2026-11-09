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


def prompt_context(*, instr: object, goal: object, feedback: object) -> dict:
    """The values of the ``openspec_artifact`` prompt's placeholders."""
    instr = _mapping(instr)
    rules = _listed(instr.get("rules"))
    rules_text = "\n".join(f"- {rule}" for rule in rules)
    feedback_text = feedback if isinstance(feedback, str) and not _blank(feedback) else ""
    return {"artifact": str(instr.get("artifactId")), "goal": str(goal), "instruction": str(instr.get("instruction")),
            "rulesSection": f"\n\nAdditional rules:\n{rules_text}" if rules else "",
            "outputPath": str(instr.get("resolvedOutputPath")), "template": str(instr.get("template")),
            "feedbackSection": (f"\n\nPrevious review feedback — address every item:\n{feedback_text}"
                                if feedback_text else "")}


def build_prompt(*, instr: object, goal: object, feedback: object) -> str:
    """Compose the prompt asking the agent to write exactly one OpenSpec artifact."""
    from common import templating
    return templating.render_default("openspec_artifact", prompt_context(instr=instr, goal=goal, feedback=feedback))

