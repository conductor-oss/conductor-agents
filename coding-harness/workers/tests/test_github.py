"""Unit tests for the ``gh``-backed gitops tasks.

Pins the exact ``gh`` argv built for ``pr_create`` and ``pr_submit_review`` and
asserts that recorded ``gh`` output parses into the expected task output. The
subprocess boundary (``common.github.run``) is mocked — no network, no real
``gh`` — and ``ensure_git_auth`` is stubbed so it makes no ``gh auth`` calls.
"""

from __future__ import annotations

import json
from pathlib import Path

from common import github
from common.exec import RunError, RunResult
from gitops.tasks import pr_checkout, pr_create, pr_submit_review


class RecordingRun:
    """Drop-in for ``common.github.run``: records each argv (and any ``--input``
    JSON payload) and returns queued results in order."""

    def __init__(self, *results):
        self.calls: list[dict] = []
        self._results: list = list(results)

    def __call__(self, cmd, cwd=None, check=True, timeout=600.0):
        payload = None
        if "--input" in cmd:
            # Capture the payload before the caller unlinks the temp file.
            payload = json.loads(Path(cmd[cmd.index("--input") + 1]).read_text())
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "check": check, "payload": payload})
        result = self._results.pop(0) if self._results else RunResult("", "", 0)
        if isinstance(result, Exception):
            raise result
        return result


def _patch(monkeypatch, rec: RecordingRun) -> None:
    monkeypatch.setattr("common.github.ensure_git_auth", lambda: True)
    monkeypatch.setattr("common.github.run", rec)


# --- pr_create ---------------------------------------------------------------

def test_pr_create_argv_and_parsing(fake_task_input, monkeypatch, load_fixture):
    rec = RecordingRun(RunResult(load_fixture("gh_pr_create_stdout.txt"), "", 0))
    _patch(monkeypatch, rec)

    task = fake_task_input(repoPath="/repo", title="Add retry with backoff",
                           body="Body text", base="main", head="fix/git-push-retry")
    result = pr_create(task)

    assert rec.calls[0]["cmd"] == [
        "gh", "pr", "create",
        "--title", "Add retry with backoff",
        "--body", "Body text",
        "--base", "main",
        "--head", "fix/git-push-retry",
    ]
    assert rec.calls[0]["cwd"] == "/repo"
    out = result.output_data
    # Number is parsed out of the URL gh prints.
    assert out["number"] == 123
    assert out["url"].endswith("/pull/123")
    assert out["draft"] is False


def test_pr_create_fill_and_draft_flags(fake_task_input, monkeypatch):
    rec = RecordingRun(RunResult("https://github.com/o/n/pull/7\n", "", 0))
    _patch(monkeypatch, rec)

    # No title + fill -> --fill (title/body derived from commits); draft -> --draft.
    task = fake_task_input(repoPath="/repo", fill="true", draft="true")
    result = pr_create(task)

    assert rec.calls[0]["cmd"] == ["gh", "pr", "create", "--fill", "--draft"]
    assert result.output_data["number"] == 7
    assert result.output_data["draft"] is True


# --- pr_checkout -------------------------------------------------------------

def test_github_pr_checkout_scopes_fork_checkout_to_upstream_pr(fake_task_input, monkeypatch):
    rec = RecordingRun(RunResult("", "", 0))
    _patch(monkeypatch, rec)
    monkeypatch.setattr("common.git._current_branch", lambda _: "fix/fork-pr")
    monkeypatch.setattr("common.git.head", lambda _: "abcdef012345")

    out = github.pr_checkout(
        "/contributor-fork", 136, pr_repo="https://github.com/upstream/project.git",
        branch="fix/fork-pr", force=True,
    )

    assert rec.calls == [{
        "cmd": ["gh", "pr", "checkout", "136", "--repo", "upstream/project",
                "--branch", "fix/fork-pr", "--force"],
        "cwd": "/contributor-fork", "check": True, "payload": None,
    }]
    assert out == {"number": 136, "branch": "fix/fork-pr", "head": "abcdef012345"}


def test_pr_checkout_uses_upstream_selector_but_keeps_local_checkout(fake_task_input, monkeypatch):
    captured = {}

    def checkout(repo_path, number, *, pr_repo=None, branch=None, force=False):
        captured.update(repo_path=repo_path, number=number, pr_repo=pr_repo,
                        branch=branch, force=force)
        return {"number": number, "branch": branch, "head": "abcdef012345"}

    monkeypatch.setattr("common.github.pr_checkout", checkout)
    result = pr_checkout(fake_task_input(
        repoPath="/contributor-fork", repo="upstream/project", number=136,
        branch="fix/fork-pr", force="true",
    ))

    assert captured == {
        "repo_path": "/contributor-fork", "number": 136,
        "pr_repo": "upstream/project", "branch": "fix/fork-pr", "force": True,
    }
    assert result.output_data["branch"] == "fix/fork-pr"


# --- pr_submit_review --------------------------------------------------------

def test_pr_submit_review_request_changes_argv_and_payload(
    fake_task_input, monkeypatch, load_fixture
):
    rec = RecordingRun(RunResult(json.dumps(load_fixture("gh_review_response.json")), "", 0))
    _patch(monkeypatch, rec)

    structured = {
        "verdict": "request_changes",
        "summary": "Please cap the backoff.",
        "comments": [{"path": "workers/common/git.py", "line": 42, "body": "cap it"}],
    }
    task = fake_task_input(repo="conductor-oss/conductor-agents", number=42,
                           structured=structured)
    result = pr_submit_review(task)

    call = rec.calls[0]
    assert call["cmd"][:5] == [
        "gh", "api", "repos/conductor-oss/conductor-agents/pulls/42/reviews",
        "--method", "POST",
    ]
    assert call["payload"] == {
        "body": "Please cap the backoff.",
        "event": "REQUEST_CHANGES",
        "comments": [{"path": "workers/common/git.py", "line": 42,
                      "side": "RIGHT", "body": "cap it"}],
    }
    out = result.output_data
    assert out["event"] == "REQUEST_CHANGES"
    assert out["inlineCount"] == 1
    assert out["inline"] is True
    assert out["url"].endswith("#pullrequestreview-987654321")


def test_pr_submit_review_never_approves(fake_task_input, monkeypatch):
    rec = RecordingRun(RunResult('{"html_url": "u"}', "", 0))
    _patch(monkeypatch, rec)

    task = fake_task_input(repo="o/n", number=1,
                           structured={"verdict": "approve", "summary": "LGTM",
                                       "comments": []})
    result = pr_submit_review(task)

    # A bot must never APPROVE — an approve verdict is downgraded to COMMENT.
    assert rec.calls[0]["payload"]["event"] == "COMMENT"
    assert result.output_data["event"] == "COMMENT"


def test_pr_submit_review_accepts_json_string_structured(fake_task_input, monkeypatch):
    rec = RecordingRun(RunResult('{"html_url": "u"}', "", 0))
    _patch(monkeypatch, rec)

    structured = json.dumps({"verdict": "comment", "summary": "S", "comments": []})
    task = fake_task_input(repo="o/n", number=2, structured=structured)
    result = pr_submit_review(task)

    assert rec.calls[0]["payload"]["event"] == "COMMENT"
    assert result.output_data["inlineCount"] == 0


def test_pr_submit_review_falls_back_to_summary_only(fake_task_input, monkeypatch):
    """If GitHub rejects inline anchoring (422), the review must still land as a
    summary-only comment with the findings folded into the body."""
    rec = RecordingRun(
        RunError("gh api reviews", 1, "", "422: line not part of the diff"),  # inline attempt
        RunResult('{"html_url": "u2"}', "", 0),                               # summary-only retry
    )
    _patch(monkeypatch, rec)

    structured = {
        "verdict": "request_changes",
        "summary": "Findings:",
        "comments": [{"path": "a.py", "line": 9999, "body": "unanchorable"}],
    }
    result = pr_submit_review(fake_task_input(repo="o/n", number=3, structured=structured))

    out = result.output_data
    assert out["inline"] is False
    assert out["inlineCount"] == 0
    # First attempt carried inline comments; retry dropped them and folded in the text.
    assert rec.calls[0]["payload"]["comments"]
    assert "comments" not in rec.calls[1]["payload"]
    assert "a.py:9999" in rec.calls[1]["payload"]["body"]
