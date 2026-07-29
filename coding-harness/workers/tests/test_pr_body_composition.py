"""Executes the embedded jq queryExpressions that compose issue_to_pr's and
address_pr's PR-facing bodies against representative sample input, so a bad edit
to the query string (not just a missing substring) is caught. Skips if the
system has no ``jq`` binary — mirrors test_openspec.py's pinned-CLI skip pattern.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WF = Path(__file__).resolve().parents[1] / "workflows"


def _load(name: str) -> dict:
    return json.loads((WF / f"{name}.json").read_text())


def _task(workflow: dict, ref: str) -> dict:
    for task in workflow["tasks"]:
        if task.get("taskReferenceName") == ref:
            return task
        for branch in (task.get("decisionCases") or {}).values():
            for child in branch:
                if child.get("taskReferenceName") == ref:
                    return child
    raise AssertionError(f"task {ref!r} not found")


def _eval_jq(query: str, data: dict, tmp_path: Path) -> str:
    prog = tmp_path / "prog.jq"
    prog.write_text(query, encoding="utf-8")
    proc = subprocess.run(["jq", "-r", "-f", str(prog)], input=json.dumps(data),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq binary not installed")


def test_issue_to_pr_compose_pr_body_includes_all_four_data_sources(tmp_path):
    workflow = _load("issue_to_pr")
    query = _task(workflow, "compose_pr_body")["inputParameters"]["queryExpression"]
    body = _eval_jq(query, {
        "issueNumber": 42,
        "proposalText": "## Why\nUsers need a greeting.\n",
        "subtasks": [{"id": "setup", "description": "add greeting"}],
        "findings": ["all requirements satisfied"],
        "verified": True,
        "totalTokens": 1234,
        "totalCostUsd": 0.42,
    }, tmp_path)
    assert "Users need a greeting." in body
    assert "- **setup**: add greeting" in body
    assert "all requirements satisfied" in body
    assert "Verified: true" in body
    assert "Tokens: 1234" in body and "Cost: $0.42" in body


def test_issue_to_pr_compose_pr_body_handles_missing_data(tmp_path):
    workflow = _load("issue_to_pr")
    query = _task(workflow, "compose_pr_body")["inputParameters"]["queryExpression"]
    body = _eval_jq(query, {"issueNumber": 1}, tmp_path)
    assert "Closes #1" in body
    assert "(no proposal text available)" in body
    assert "_none_" in body


def test_address_pr_compose_reply_code_parallel_path_includes_all_four_data_sources(tmp_path):
    workflow = _load("address_pr")
    query = _task(workflow, "compose_reply")["inputParameters"]["queryExpression"]
    body = _eval_jq(query, {
        "engine": "code_parallel", "prNumber": 9, "head": "fix-branch",
        "proposalText": "## Why\nBug fix.\n",
        "subtasks": [{"id": "x", "description": "fix x"}],
        "findings": ["clean"], "verified": True,
        "totalTokens": 10, "totalCostUsd": 0.01,
    }, tmp_path)
    assert "Bug fix." in body
    assert "- **x**: fix x" in body
    assert "clean" in body
    assert "Verified: true" in body
    assert "Tokens: 10" in body and "Cost: $0.01" in body


def test_address_pr_compose_reply_coding_agent_path_uses_agent_result(tmp_path):
    workflow = _load("address_pr")
    query = _task(workflow, "compose_reply")["inputParameters"]["queryExpression"]
    body = _eval_jq(query, {
        "engine": "coding_agent", "prNumber": 9, "head": "fix-branch",
        "agentResult": "Fixed the typo and updated tests.",
    }, tmp_path)
    assert "Fixed the typo and updated tests." in body
    assert "coding_agent" in body
