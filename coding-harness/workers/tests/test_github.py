"""Unit tests for the ``gh``-backed gitops tasks.

Pins the exact ``gh`` argv built for ``pr_create`` and ``pr_submit_review`` and
asserts that recorded ``gh`` output parses into the expected task output. The
subprocess boundary (``common.github.run``) is mocked — no network, no real
``gh`` — and ``ensure_git_auth`` is stubbed so it makes no ``gh auth`` calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import common.github as github
from common.exec import RunError, RunResult
from common.github import pr_comments, pr_diff, submit_review
from gitops.tasks import pr_create, pr_submit_review


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


# --- PR_DIFF_CAP override ----------------------------------------------------

class QueuedRun:
    """Returns queued RunResults in call order, ignoring argv (for helpers that
    make more than one ``gh`` call)."""

    def __init__(self, *results):
        self.calls: list[list[str]] = []
        self._results = list(results)

    def __call__(self, cmd, cwd=None, check=True, timeout=600.0):
        self.calls.append(list(cmd))
        return self._results.pop(0) if self._results else RunResult("", "", 0)


def test_pr_diff_cap_env_override_changes_truncation(monkeypatch):
    """``PR_DIFF_CAP`` shrinks the truncation threshold pr_diff applies."""
    monkeypatch.setattr("common.github.ensure_git_auth", lambda: True)
    long_diff = "x" * 500
    rec = QueuedRun(
        RunResult(json.dumps({"baseRefName": "main", "files": [{"path": "a.py"}]}), "", 0),
        RunResult(long_diff, "", 0),
    )
    monkeypatch.setattr("common.github.run", rec)

    monkeypatch.setenv("PR_DIFF_CAP", "50")
    out = pr_diff("o/n", 1)
    assert out["truncated"] is True
    # Capped body = first 50 chars + the truncation marker.
    assert out["diff"] == long_diff[:50] + "\n…[diff truncated]"


def test_pr_diff_cap_env_invalid_falls_back_to_default(monkeypatch):
    """Non-numeric / non-positive PR_DIFF_CAP falls back to the 200000 default."""
    monkeypatch.setenv("PR_DIFF_CAP", "not-a-number")
    assert github._diff_cap() == 200_000
    monkeypatch.setenv("PR_DIFF_CAP", "0")
    assert github._diff_cap() == 200_000
    monkeypatch.setenv("PR_DIFF_CAP", "-5")
    assert github._diff_cap() == 200_000
    monkeypatch.delenv("PR_DIFF_CAP", raising=False)
    assert github._diff_cap() == 200_000
    monkeypatch.setenv("PR_DIFF_CAP", "123")
    assert github._diff_cap() == 123


# --- null / None hardening ---------------------------------------------------

def test_pr_comments_null_author_does_not_raise(monkeypatch):
    """A comment/review with an explicit JSON null ``author`` must not raise in the
    _keep/author-extraction path — the login falls back to '?'."""
    monkeypatch.setattr("common.github.ensure_git_auth", lambda: True)
    meta = {
        "number": 7, "title": "T", "headRefName": "h", "baseRefName": "main", "url": "u",
        "headRepositoryOwner": None, "headRepository": None,
        "comments": [{"author": None, "body": "please fix"}],
        "reviews": [{"author": None, "state": "COMMENT", "body": "reviewed"}],
    }
    inline = [{"user": None, "path": "a.py", "line": 3, "body": "inline note"}]
    rec = QueuedRun(
        RunResult(json.dumps(meta), "", 0),        # gh pr view --json
        RunResult(json.dumps(inline), "", 0),      # gh api .../comments
    )
    monkeypatch.setattr("common.github.run", rec)

    out = pr_comments("o/n", 7)
    assert out["commentCount"] == 3
    assert out["hasFeedback"] is True
    assert "@?" in out["feedback"]           # null author rendered as the '?' fallback
    assert out["headRepoUrl"] == ""          # null head repo → empty, not a crash


def test_submit_review_skips_uncoercible_line(fake_task_input, monkeypatch):
    """A non-integer/None inline ``line`` is skipped rather than sinking the review."""
    monkeypatch.setattr("common.github.ensure_git_auth", lambda: True)
    rec = QueuedRun(RunResult('{"html_url": "u"}', "", 0))
    monkeypatch.setattr("common.github.run", rec)

    out = submit_review(
        "o/n", 1, summary="S", event="COMMENT",
        comments=[
            {"path": "a.py", "line": "not-int", "body": "bad"},
            {"path": "b.py", "line": None, "body": "also bad"},
            {"path": "c.py", "line": 12, "body": "good"},
        ],
    )
    # Only the coercible comment survives; the review still lands.
    assert out["inlineCount"] == 1
    assert out["inline"] is True
