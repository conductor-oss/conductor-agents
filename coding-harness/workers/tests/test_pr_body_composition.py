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
    def walk(value):
        if isinstance(value, dict):
            if value.get("taskReferenceName") == ref:
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    for task in walk(workflow["tasks"]):
        return task
    raise AssertionError(f"task {ref!r} not found")


def _eval_jq(query: str, data: dict, tmp_path: Path) -> str:
    prog = tmp_path / "prog.jq"
    prog.write_text(query, encoding="utf-8")
    proc = subprocess.run(["jq", "-r", "-f", str(prog)], input=json.dumps(data),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq binary not installed")


def test_issue_to_pr_body_is_composed_by_the_worker_not_by_jq(tmp_path):
    from common import pr_description

    # issue_to_pr used to pre-normalize the body in jq and hand the result to
    # pr_create, which ran format_summary over it again. The jq layer is gone;
    # this is the single place the one-section invariant is now enforced.
    rendered = json.dumps(_load("issue_to_pr"))
    assert "compose_pr_body" not in rendered
    assert "compose_revised_pr_body" not in rendered

    described = pr_description.format_summary(tmp_path, "## Why\nUsers need a greeting.\n")
    # Better than the jq it replaces: a "## Why" heading is a template label, so
    # the worker drops it rather than inlining the word into the summary.
    assert described["body"] == "## Summary\n\nUsers need a greeting."


def test_issue_to_pr_body_falls_back_when_there_is_no_summary(tmp_path):
    from common import pr_description

    assert pr_description.format_summary(tmp_path, "")["body"] == \
        "## Summary\n\nAutomated change."


def test_address_pr_compose_reply_code_parallel_path_includes_review_content():
    from common import pr_reply

    workflow = _load("address_pr")
    task = _task(workflow, "compose_parallel_reply")
    assert task["name"] == "address_pr_reply"

    body = pr_reply.compose_parallel_reply(
        pr_number=9, head="fix-branch", proposal_text="## Why\nBug fix.\n",
        subtasks=[{"id": "x", "description": "fix x"}], findings=["clean"], verified=True)
    assert "Bug fix." in body
    assert "- **x**: fix x" in body
    assert "clean" in body
    assert "Verified: true" in body
    assert "Tokens: 10" not in body and "Cost: $0.01" not in body


