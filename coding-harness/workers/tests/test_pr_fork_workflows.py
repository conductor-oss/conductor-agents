from __future__ import annotations

import json
from pathlib import Path


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


def _walk(value):
    if isinstance(value, dict):
        if "taskReferenceName" in value:
            yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _task(workflow: dict, reference: str) -> dict:
    return next(task for task in _walk(workflow) if task["taskReferenceName"] == reference)


def _load(name: str) -> dict:
    return json.loads((WORKFLOWS / f"{name}.json").read_text(encoding="utf-8"))


def test_pr_review_clones_upstream_and_resolves_the_pr_against_it():
    workflow = _load("pr_review")
    assert _task(workflow, "clone")["inputParameters"]["repoUrl"] == "${workflow.input.repo}"
    checkout = _task(workflow, "co")["inputParameters"]
    assert checkout["repo"] == "${workflow.input.repo}"


def test_address_pr_preserves_fork_origin_but_uses_upstream_pr_metadata():
    workflow = _load("address_pr")
    assert _task(workflow, "clone")["inputParameters"]["repoUrl"] == "${fb.output.headRepoUrl}"
    checkout = _task(workflow, "co")["inputParameters"]
    assert checkout["repo"] == "${workflow.input.repo}"
    assert checkout["branch"] == "${fb.output.head}"
