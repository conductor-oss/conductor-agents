"""Codex structured-output schema normalization.

Guards the strict-mode contract: every workflow `schema` block is handed to Codex
verbatim, and the OpenAI validator answers 400 invalid_json_schema for keywords it
does not accept (`uniqueItems` is the one that took down document_plan). Any schema
shipped in workers/workflows/ must survive `_strictify_schema`.
"""

from __future__ import annotations

import json
from pathlib import Path

from common import architect, program, retrospective
from common.codex import _UNSUPPORTED_KEYWORDS, _omit_strict_nulls, _restore_optional, _strictify_schema
from team import tasks as team_tasks

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


def test_strictify_drops_keywords_the_validator_rejects():
    schema = {
        "type": "object",
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
        },
    }

    out = _strictify_schema(schema)

    assert "uniqueItems" not in out["properties"]["files"]
    # Constraints the validator *does* accept must survive untouched.
    assert out["properties"]["files"]["minItems"] == 1
    assert out["properties"]["files"]["items"]["minLength"] == 1


def test_strictify_drops_unsupported_keywords_at_every_depth():
    nested = _strictify_schema({
        "type": "object",
        "required": ["outer"],
        "properties": {
            "outer": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["inner"],
                    "propertyNames": {"pattern": "^x$"},
                    "properties": {"inner": {"type": "array", "uniqueItems": True}},
                },
            },
        },
    })

    item = nested["properties"]["outer"]["items"]
    assert "propertyNames" not in item
    assert "uniqueItems" not in item["properties"]["inner"]


def test_strictify_preserves_object_strictness_contract():
    out = _strictify_schema({
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    })

    assert out["additionalProperties"] is False
    assert sorted(out["required"]) == ["a", "b"]
    # `b` was optional, so it stays omittable by becoming nullable.
    assert out["properties"]["b"]["type"] == ["string", "null"]
    assert out["properties"]["a"]["type"] == "string"


def test_shipped_workflow_schemas_are_codex_safe():
    checked = []
    for path in sorted(WORKFLOWS.rglob("*.json")):
        definition = json.loads(path.read_text())
        for task in definition.get("tasks", []):
            schema = (task.get("inputParameters") or {}).get("schema")
            if not isinstance(schema, dict):
                continue
            checked.append(f"{path.name}:{task.get('taskReferenceName')}")
            rendered = json.dumps(_strictify_schema(schema))
            for keyword in _UNSUPPORTED_KEYWORDS:
                assert f'"{keyword}"' not in rendered, (
                    f"{path.name}:{task.get('taskReferenceName')} keeps {keyword}"
                )

    assert checked, "no workflow task schemas found — the guard would be vacuous"


def _open_objects(node, path="$"):
    """Every object the OpenAI strict validator would refuse: it must be closed at every depth."""
    found = []
    if isinstance(node, dict):
        kinds = node.get("type") if isinstance(node.get("type"), list) else [node.get("type")]
        if "object" in kinds and node.get("additionalProperties") is not False:
            found.append(path)
        for key, value in node.items():
            found += _open_objects(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _open_objects(value, f"{path}[{index}]")
    return found


def _workflow_schemas(tasks, name):
    for task in tasks:
        schema = (task.get("inputParameters") or {}).get("schema")
        if isinstance(schema, dict):
            yield f"{name}:{task.get('taskReferenceName')}", schema
        for branch in (task.get("decisionCases") or {}).values():
            yield from _workflow_schemas(branch, name)
        for key in ("defaultCase", "loopOver"):
            yield from _workflow_schemas(task.get(key) or [], name)
        for fork in task.get("forkTasks") or []:
            yield from _workflow_schemas(fork, name)


def test_every_swarm_schema_is_closed_for_openai_strict_mode_at_every_depth():
    """Run 0774108e: the planner's subtask items were a bare {"type": "object"}, which strict mode
    refuses with a 400, so no swarm could plan on a Codex profile. A bare object cannot be normalized
    (strict mode has no open object), so each one has to declare its fields."""
    schemas = dict(s for path in sorted(WORKFLOWS.glob("*.json"))
                   for s in _workflow_schemas(json.loads(path.read_text()).get("tasks", []), path.name))
    schemas.update({"implementer": team_tasks.IMPLEMENTER_SCHEMA, "reviewer": team_tasks.REVIEWER_SCHEMA,
                    "breakdown": program.BREAKDOWN_SCHEMA, "retrospective": retrospective.SCHEMA,
                    "architect.draft": architect.DRAFT_SCHEMA, "architect.scout": architect.SCOUT_SCHEMA,
                    "architect.review": architect.REVIEW_SCHEMA,
                    "architect.evaluate": architect.EVALUATE_SCHEMA, "architect.opinion": architect.OPINION_SCHEMA})
    assert {"cc_program.json:program_plan", "sdlc_intake.json:classify", "implementer"} <= set(schemas)
    assert {name: _open_objects(_strictify_schema(s)) for name, s in schemas.items()
            if _open_objects(_strictify_schema(s))} == {}


def test_strictify_closes_nullable_objects_and_keeps_optional_enums_omittable():
    out = _strictify_schema({"type": "object", "required": [], "properties": {
        "pushback": {"type": ["object", "null"], "properties": {"reason": {"type": "string"}}},
        "kind": {"type": "string", "enum": ["code", "docs"]}}})
    assert out["properties"]["pushback"]["additionalProperties"] is False
    assert out["properties"]["pushback"]["required"] == ["reason"]
    assert out["properties"]["kind"] == {"type": ["string", "null"], "enum": ["code", "docs", None]}


def test_strict_mode_nulls_read_as_absent_so_an_empty_pushback_is_not_a_pushback():
    schema = {"type": "object", "required": ["summary", "must"], "properties": {
        "summary": {"type": "string"}, "must": {"type": ["string", "null"]},
        "pushback": {"type": ["object", "null"], "properties": {"directive": {"type": "string"}}},
        "items": {"type": "array", "items": {"type": "object", "required": ["id"], "properties": {
            "id": {"type": "string"}, "dependsOn": {"type": "array", "items": {"type": "string"}}}}}}}
    got = {"summary": "s", "must": None, "pushback": {"directive": None},
           "items": [{"id": "a", "dependsOn": None}]}
    assert _omit_strict_nulls(got, schema) == {"summary": "s", "must": None, "pushback": {},
                                               "items": [{"id": "a"}]}
    restored = _restore_optional({"structured": got, "result": "x"}, schema)
    assert json.loads(restored["result"]) == restored["structured"]
    untouched = {"structured": None, "result": "plain"}
    assert _restore_optional(untouched, schema) is untouched
