"""Every agent step of every workflow runs from a shipped default prompt of its own.

Steps without one fell through to whatever ``code.md`` said: planners, classifiers and reviewers got
coding instructions appended, and a step passing ``{"subtask": description}`` lost its whole composed
prompt, which is how team implementers ran without their target files or frozen contract."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from common import program, retrospective, templating as t

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
DEFAULTS = Path(__file__).resolve().parents[1] / "defaults" / "prompts"
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def _steps():
    def walk(tasks, name):
        for task in tasks:
            if task.get("name") == "coding_agent":
                yield name, task
            for branch in (task.get("decisionCases") or {}).values():
                yield from walk(branch, name)
            for key in ("defaultCase", "loopOver"):
                yield from walk(task.get(key) or [], name)
            for fork in task.get("forkTasks") or []:
                yield from walk(fork, name)
    for path in sorted(WORKFLOWS.glob("*.json")):
        definition = json.loads(path.read_text())
        yield from walk(definition.get("tasks", []), definition["name"])


STEPS = list(_steps())


def test_the_inventory_is_real():
    assert len(STEPS) >= 29


@pytest.mark.parametrize("workflow,task", STEPS, ids=[f"{w}.{s['taskReferenceName']}" for w, s in STEPS])
def test_every_agent_step_has_a_shipped_default(workflow, task):
    key = task["inputParameters"].get("templateKey")
    assert key, "no templateKey, so no default, repository override or Settings entry can reach this step"
    if str(key).startswith("${"):
        assert (workflow, task["taskReferenceName"]) == ("code_subtask", "code")  # chosen by each caller
        return
    assert t.bundled_default(key), f"no workers/defaults/prompts/{key}.md"


@pytest.mark.parametrize("workflow,task", STEPS, ids=[f"{w}.{s['taskReferenceName']}" for w, s in STEPS])
def test_every_placeholder_of_a_steps_default_is_supplied(workflow, task):
    params = task["inputParameters"]
    key, context = params.get("templateKey"), params.get("promptContext")
    if str(key).startswith("${") or isinstance(context, str):
        return  # the context is computed at run time; the code-built tests below cover those
    wanted = set(PLACEHOLDER.findall(t.bundled_default(key))) - {"subtask"}
    assert wanted <= set(context or {}), f"{key} needs {sorted(wanted - set(context or {}))}"


def test_steps_that_are_not_coding_no_longer_inherit_the_coding_prompt():
    keys = {(w, s["taskReferenceName"]): s["inputParameters"].get("templateKey") for w, s in STEPS}
    for step in (("cc_program", "program_plan"), ("sdlc_intake", "classify"), ("run_retrospective", "retro_agent"),
                 ("cc_program", "rank_breakdown"), ("project_bootstrap", "scaffold"), ("bug_fix", "repro")):
        assert keys[step] != "code", step


def test_delivery_repair_keeps_its_repair_instructions():
    """Sharing the subtask's key and context, the repair agent got the original description back."""
    repair = next(s for w, s in STEPS if s["taskReferenceName"] == "delivery_repair")["inputParameters"]
    assert repair["templateKey"] == "delivery_repair" and repair["promptTemplate"] == ""
    assert "NOT ACCEPTED" in t.bundled_default("delivery_repair")


def test_a_composed_prompt_renders_completely_from_its_default():
    context = program.breakdown_context(description="the program", docs=["docs/a.md"], remaining=[{"id": "x"}],
                                        contract_paths=[], prior="")
    text = program.breakdown_prompt(description="the program", docs=["docs/a.md"], remaining=[{"id": "x"}],
                                    contract_paths=[], prior="")
    assert t.render_default("program_breakdown", context) == text
    assert "- docs/a.md" in text and '"id": "x"' in text and not PLACEHOLDER.search(text)
    retro = retrospective.prompt({"verdict": "DELIVERED"}, languages=["go"], host_name="h")
    assert "Run verdict: DELIVERED" in retro and "Repository languages: go; host: h" in retro
    assert not PLACEHOLDER.search(retro)


def test_a_repository_can_override_a_swarm_step(tmp_path):
    (tmp_path / ".conductor").mkdir()
    (tmp_path / ".conductor" / "team_review.md").write_text("Review {{item}} for security only.")
    res = t.resolve_prompt_details("inline", template="", template_key="team_review",
                                   context={"item": "a"}, worktree=str(tmp_path))
    assert res.prompt == "Review a for security only." and res.source == "repo:.conductor/team_review.md"


def test_rendering_is_one_pass_and_context_values_are_never_placeholders():
    out = t.render_template("A={{a}} B={{b}}", {"a": "{{b}}", "b": "bee"})
    assert out == "A={{b}} B=bee"
    assert t.render_template("{{x}}", {"x": ["a", "b"]}) == '["a", "b"]'


def test_a_value_the_engine_dropped_is_blank_in_a_shipped_default():
    res = t.resolve_prompt_details("inline", template="", template_key="intake_classify",
                                   context={"request": "r"}, worktree="/nonexistent")
    assert "{{" not in res.prompt and res.source == "bundled:intake_classify"
