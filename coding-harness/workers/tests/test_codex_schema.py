"""Codex structured-output schema normalization.

Guards the strict-mode contract: every workflow `schema` block is handed to Codex
verbatim, and the OpenAI validator answers 400 invalid_json_schema for keywords it
does not accept (`uniqueItems` is the one that took down document_plan). Any schema
shipped in workers/workflows/ must survive `_strictify_schema`.
"""

from __future__ import annotations

import json
from pathlib import Path

from common.codex import _UNSUPPORTED_KEYWORDS, _strictify_schema

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
