from __future__ import annotations

import json
from pathlib import Path


WF = Path(__file__).resolve().parents[1] / "workflows"
FLOWS = (
    "feature_campaign", "openspec_development",
)


def _load(name: str) -> dict:
    return json.loads((WF / f"{name}.json").read_text())


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_all_coding_flows_publish_workspace_contract():
    for name in FLOWS:
        workflow = _load(name)
        assert workflow["version"] == 2
        assert workflow["inputTemplate"]["keepWorktree"] is True
        assert workflow["inputTemplate"]["workspacePath"] == ""
        serialized = json.dumps(workflow)
        assert '"name": "workspace_prepare"' in serialized
        assert '"name": "workspace_cleanup"' in serialized
        assert "worktreePath" in workflow["outputParameters"]
        assert "workspaceRetained" in workflow["outputParameters"]


def test_nested_workflows_pin_current_versions_and_inherit_parent_workspace():
    for name in ("issue_to_pr", "address_pr", "openspec_development"):
        workflow = _load(name)
        children = [node for node in _walk(workflow) if node.get("type") == "SUB_WORKFLOW"]
        assert children
        for child in children:
            target = child["subWorkflowParam"]["name"]
            assert child["subWorkflowParam"]["version"] == (3 if target == "code_parallel" else 2)
        if name == "openspec_development":
            assert "workspacePath" in json.dumps(workflow)

    code_parallel = _load("code_parallel")
    build_forks = next(node for node in _walk(code_parallel)
                       if node.get("taskReferenceName") == "build_forks")
    assert 'subWorkflowParam:{name:"code_subtask", version:2}' in \
        build_forks["inputParameters"]["queryExpression"]


def test_github_flows_require_a_remote_repository_identifier():
    for name in ("pr_review", "issue_to_pr", "address_pr"):
        workflow = _load(name)
        assert "repo" in workflow["inputParameters"]
        assert "repoPath" not in workflow["inputTemplate"]
