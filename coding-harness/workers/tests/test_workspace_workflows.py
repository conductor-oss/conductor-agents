from __future__ import annotations

import json
from pathlib import Path


WF = Path(__file__).resolve().parents[1] / "workflows"
FLOWS = (
    "pr_review", "issue_to_pr", "address_pr", "code_parallel", "design_docs",
    "feature_campaign", "openspec_development", "github_demo",
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
        expected = 3 if name in {"pr_review", "issue_to_pr", "address_pr", "code_parallel"} else 2
        assert workflow["version"] == expected
        assert workflow["inputTemplate"]["keepWorktree"] is True
        assert workflow["inputTemplate"]["workspacePath"] == ""
        serialized = json.dumps(workflow)
        assert '"name": "workspace_prepare"' in serialized
        assert '"name": "workspace_cleanup"' in serialized
        assert "worktreePath" in workflow["outputParameters"]
        assert "workspaceRetained" in workflow["outputParameters"]


def test_nested_workflows_pin_current_versions_and_inherit_parent_workspace():
    for name in ("code_parallel", "issue_to_pr", "address_pr", "openspec_development"):
        workflow = _load(name)
        children = [node for node in _walk(workflow) if node.get("type") == "SUB_WORKFLOW"]
        assert children
        for child in children:
            target = child["subWorkflowParam"]["name"]
            expected = 3 if target == "code_parallel" else 2
            assert child["subWorkflowParam"]["version"] == expected
        if name != "code_parallel":
            assert "workspacePath" in json.dumps(workflow)


def test_github_flows_accept_optional_local_checkout():
    for name in ("pr_review", "issue_to_pr", "address_pr"):
        workflow = _load(name)
        assert workflow["inputTemplate"]["repoPath"] == ""
        prepare = next(node for node in _walk(workflow)
                       if node.get("name") == "workspace_prepare")
        assert prepare["inputParameters"]["expectedRepos"]
        assert prepare["inputParameters"]["fetchRefspec"]
