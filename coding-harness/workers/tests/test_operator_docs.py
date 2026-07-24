"""Smoke tests for the public/operator documentation map."""

import json
from pathlib import Path

from common.model_policy import validate_policy


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"


def test_operator_guides_are_indexed_and_present():
    index = (DOCS / "index.md").read_text(encoding="utf-8")
    for name in ("model-profiles.md", "templates.md", "openspec.md"):
        assert (DOCS / name).is_file()
        assert name in index


def test_model_policy_example_uses_the_active_declarative_schema():
    policy = json.loads((DOCS / "config" / "models.example.json").read_text(encoding="utf-8"))
    validate_policy(policy)
    assert policy["defaultProfile"] in policy["profiles"]
