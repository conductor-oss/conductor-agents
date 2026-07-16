"""Unit tests for the ``gh``-backed gitops tasks.

Pins the exact ``gh`` argv built for ``pr_create`` and ``pr_submit_review`` and
asserts that recorded ``gh`` output parses into the expected task output. The
subprocess boundary (``common.github.run``) is mocked — no network, no real
``gh`` — and ``ensure_git_auth`` is stubbed so it makes no ``gh auth`` calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import common.github as github
from common.exec import RunError, RunResult
from common.github import pr_comments, pr_diff, render_review_markdown, submit_review
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
    # Self-review detection is undetectable here (no reviewer/author) so submit_review
    # takes the formal reviews-API path — keeps these argv assertions on rec.calls[0].
    monkeypatch.setattr("common.github.viewer_login", lambda: None)
    monkeypatch.setattr("common.github.pr_author_login", lambda *a, **k: None)


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
    monkeypatch.setattr("common.github.viewer_login", lambda: None)
    monkeypatch.setattr("common.github.pr_author_login", lambda *a, **k: None)
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


# --- self-review 422 fix -----------------------------------------------------

def _reviews_calls(rec: RecordingRun) -> list:
    """gh calls that hit the formal reviews API (to assert it wasn't touched)."""
    return [c for c in rec.calls if "/reviews" in " ".join(c["cmd"])]


def _no_pr_comment():
    raise AssertionError("pr_comment must not be called in reviews mode")


def test_submit_review_self_review_goes_to_comments_mode(monkeypatch):
    """Viewer == author → the reviews API is NEVER touched. Findings post as inline
    review comments (POST .../pulls/<n>/comments with the head sha) plus one
    conversation summary comment via pr_comment. Comparison is case-insensitive."""
    monkeypatch.setattr("common.github.ensure_git_auth", lambda: True)
    monkeypatch.setattr("common.github.viewer_login", lambda: "bot")
    monkeypatch.setattr("common.github.pr_author_login", lambda *a, **k: "Bot")
    conv: list = []
    monkeypatch.setattr(
        "common.github.pr_comment",
        lambda repo, number, body: conv.append((repo, number, body))
        or {"commented": True, "number": number, "url": "https://c/summary"},
    )
    rec = RecordingRun(
        RunResult(json.dumps({"headRefOid": "abc123"}), "", 0),  # head sha lookup
        RunResult('{"id": 1}', "", 0),                           # inline comment POST
    )
    monkeypatch.setattr("common.github.run", rec)

    out = submit_review("o/n", 5, summary="Please fix", event="REQUEST_CHANGES",
                        comments=[{"path": "a.py", "line": 10, "body": "nit"}])

    assert out["mode"] == "comments"
    assert out["selfReview"] is True
    assert out["reviewed"] is True
    assert out["inlineCount"] == 1
    assert out["inline"] is True
    assert out["url"] == "https://c/summary"
    # Reviews API never hit.
    assert _reviews_calls(rec) == []
    # Head sha resolved, then the inline comment POSTed with the exact payload.
    assert rec.calls[0]["cmd"] == ["gh", "pr", "view", "5", "--repo", "o/n",
                                   "--json", "headRefOid"]
    assert rec.calls[1]["cmd"][:5] == ["gh", "api", "repos/o/n/pulls/5/comments",
                                       "--method", "POST"]
    assert rec.calls[1]["payload"] == {
        "commit_id": "abc123", "path": "a.py", "line": 10, "side": "RIGHT",
        "body": "nit\n\n" + github.HARNESS_MARKER,
    }
    # One conversation summary comment landed (repo-slug scoped).
    assert conv and conv[0][0] == "o/n" and conv[0][1] == 5


def test_submit_review_different_users_stays_review_mode(monkeypatch):
    """Viewer != author → formal reviews API, and the existing inline-drop 422
    fallback still lands a summary-only review (mode stays 'review')."""
    monkeypatch.setattr("common.github.ensure_git_auth", lambda: True)
    monkeypatch.setattr("common.github.viewer_login", lambda: "reviewer")
    monkeypatch.setattr("common.github.pr_author_login", lambda *a, **k: "author")
    monkeypatch.setattr("common.github.pr_comment", lambda *a, **k: _no_pr_comment())
    rec = RecordingRun(
        RunError("gh api reviews", 1, "", "422: line not part of the diff"),  # inline attempt
        RunResult('{"html_url": "u2"}', "", 0),                               # summary-only retry
    )
    monkeypatch.setattr("common.github.run", rec)

    out = submit_review("o/n", 3, summary="Findings:", event="REQUEST_CHANGES",
                        comments=[{"path": "a.py", "line": 9999, "body": "unanchorable"}])

    assert out["mode"] == "review"
    assert out["selfReview"] is False
    assert out["inline"] is False
    assert out["inlineCount"] == 0
    assert rec.calls[0]["payload"]["comments"]        # first attempt carried inline
    assert "comments" not in rec.calls[1]["payload"]  # retry dropped them
    assert "a.py:9999" in rec.calls[1]["payload"]["body"]


def test_submit_review_reviews_api_self_review_422_falls_back(monkeypatch):
    """Viewer/author undetectable (both None): the reviews POST 422s with a
    self-review hint → fall back to comments mode with selfReview True."""
    monkeypatch.setattr("common.github.ensure_git_auth", lambda: True)
    monkeypatch.setattr("common.github.viewer_login", lambda: None)
    monkeypatch.setattr("common.github.pr_author_login", lambda *a, **k: None)
    conv: list = []
    monkeypatch.setattr(
        "common.github.pr_comment",
        lambda repo, number, body: conv.append((repo, number, body))
        or {"commented": True, "number": number, "url": "https://c/self"},
    )
    rec = RecordingRun(
        RunError("gh api reviews", 1, "",
                 "HTTP 422: Review cannot be submitted on your own pull request"),  # reviews POST
        RunResult(json.dumps({"headRefOid": "sha1"}), "", 0),                        # head sha
        RunResult('{"id": 2}', "", 0),                                               # inline POST
    )
    monkeypatch.setattr("common.github.run", rec)

    out = submit_review("o/n", 8, summary="S", event="COMMENT",
                        comments=[{"path": "a.py", "line": 4, "body": "note"}])

    assert out["mode"] == "comments"
    assert out["selfReview"] is True
    assert out["url"] == "https://c/self"
    assert out["inlineCount"] == 1
    # The reviews API WAS attempted (once) before falling back.
    assert rec.calls[0]["cmd"][:3] == ["gh", "api", "repos/o/n/pulls/8/reviews"]
    assert rec.calls[1]["cmd"] == ["gh", "pr", "view", "8", "--repo", "o/n",
                                   "--json", "headRefOid"]
    assert conv


def test_submit_review_non_self_review_line_not_in_diff_unchanged(monkeypatch):
    """A line-not-in-diff 422 (text NOT matching the self-review hints) still triggers
    the summary-only inline-drop retry — mode stays 'review', no comments fallback."""
    monkeypatch.setattr("common.github.ensure_git_auth", lambda: True)
    monkeypatch.setattr("common.github.viewer_login", lambda: None)
    monkeypatch.setattr("common.github.pr_author_login", lambda *a, **k: None)
    monkeypatch.setattr("common.github.pr_comment", lambda *a, **k: _no_pr_comment())
    rec = RecordingRun(
        RunError("gh api reviews", 1, "",
                 "422: pull_request_review_thread.line must be part of the diff"),
        RunResult('{"html_url": "u9"}', "", 0),
    )
    monkeypatch.setattr("common.github.run", rec)

    out = submit_review("o/n", 4, summary="Findings:", event="COMMENT",
                        comments=[{"path": "a.py", "line": 9999, "body": "x"}])

    assert out["mode"] == "review"
    assert out["selfReview"] is False
    assert out["inline"] is False
    assert out["inlineCount"] == 0
    assert out["url"] == "u9"
    assert "comments" not in rec.calls[1]["payload"]


# --- local review-output file ------------------------------------------------

def test_submit_review_writes_local_file_relative_and_absolute(monkeypatch, tmp_path):
    """local_output_path is written (parents created) before posting; localOutputPath
    in the result is the absolute path, for both relative+repo_path and absolute."""
    monkeypatch.setattr("common.github.ensure_git_auth", lambda: True)
    monkeypatch.setattr("common.github.viewer_login", lambda: None)
    monkeypatch.setattr("common.github.pr_author_login", lambda *a, **k: None)

    # Relative path resolved under repo_path (parent dirs created).
    monkeypatch.setattr("common.github.run", RecordingRun(RunResult('{"html_url": "u"}', "", 0)))
    repo_path = str(tmp_path / "checkout")
    out = submit_review("o/n", 1, summary="Body", event="COMMENT",
                        comments=[{"path": "a.py", "line": 5, "body": "c"}],
                        local_output_path=".conductor/review-output.md", repo_path=repo_path)
    expected = os.path.abspath(os.path.join(repo_path, ".conductor", "review-output.md"))
    assert out["localOutputPath"] == expected
    assert Path(expected).read_text() == render_review_markdown(
        "Body", "COMMENT", [{"path": "a.py", "line": 5, "body": "c"}])
    assert out["reviewed"] is True

    # Absolute path used as-is; parent dir created.
    monkeypatch.setattr("common.github.run", RecordingRun(RunResult('{"html_url": "u"}', "", 0)))
    abs_target = str(tmp_path / "nested" / "dir" / "out.md")
    out2 = submit_review("o/n", 1, summary="S", event="COMMENT", comments=[],
                         local_output_path=abs_target)
    assert out2["localOutputPath"] == os.path.abspath(abs_target)
    assert Path(abs_target).read_text() == render_review_markdown("S", "COMMENT", [])


def test_submit_review_no_local_file_when_option_off(monkeypatch, tmp_path):
    """No local_output_path → nothing written, localOutputPath == ''."""
    monkeypatch.setattr("common.github.ensure_git_auth", lambda: True)
    monkeypatch.setattr("common.github.viewer_login", lambda: None)
    monkeypatch.setattr("common.github.pr_author_login", lambda *a, **k: None)
    monkeypatch.setattr("common.github.run", RecordingRun(RunResult('{"html_url": "u"}', "", 0)))

    out = submit_review("o/n", 1, summary="S", event="COMMENT", comments=[],
                        local_output_path=None, repo_path=str(tmp_path))
    assert out["localOutputPath"] == ""
    assert list(tmp_path.iterdir()) == []


def test_submit_review_local_file_write_failure_is_logged_not_raised(monkeypatch, tmp_path):
    """A write failure (parent is a regular file) is logged, not raised — posting
    still proceeds and localOutputPath is ''."""
    monkeypatch.setattr("common.github.ensure_git_auth", lambda: True)
    monkeypatch.setattr("common.github.viewer_login", lambda: None)
    monkeypatch.setattr("common.github.pr_author_login", lambda *a, **k: None)
    rec = RecordingRun(RunResult('{"html_url": "u"}', "", 0))
    monkeypatch.setattr("common.github.run", rec)

    blocker = tmp_path / "afile"
    blocker.write_text("x")                       # a file where a dir is needed
    bad_path = str(blocker / "sub" / "out.md")

    out = submit_review("o/n", 1, summary="S", event="COMMENT", comments=[],
                        local_output_path=bad_path)

    assert out["localOutputPath"] == ""           # write failed, swallowed
    assert out["reviewed"] is True                # posting proceeded despite the failure
    assert _reviews_calls(rec)                     # reviews API was posted to


# --- render_review_markdown --------------------------------------------------

def test_render_review_markdown_format():
    """Snapshot the badge / summary / bullets / trailing marker layout."""
    marker = github.HARNESS_MARKER
    # No comments → findings section omitted.
    assert render_review_markdown("Looks good.", "COMMENT", []) == (
        "## Automated review — ✅ comment\n\nLooks good.\n\n" + marker
    )
    # Anchorable + unanchorable findings (missing line → `path`).
    assert render_review_markdown("Please fix.", "REQUEST_CHANGES", [
        {"path": "a.py", "line": 12, "body": "cap it"},
        {"path": "b.py", "body": "no line"},
    ]) == (
        "## Automated review — 🔧 request changes\n\n"
        "Please fix.\n\n"
        "### Inline findings\n\n"
        "- `a.py:12` — cap it\n"
        "- `b.py` — no line\n\n"
        + marker
    )
    # Empty summary → placeholder; section still omitted with comments=None.
    md = render_review_markdown("", "COMMENT", None)
    assert md == "## Automated review — ✅ comment\n\nNo summary provided.\n\n" + marker
    assert "### Inline findings" not in md


# --- viewer_login caching + safety -------------------------------------------

def test_viewer_login_caches_and_is_safe(monkeypatch):
    """viewer_login resolves once per process (second call issues no extra gh call);
    a non-zero `gh api user` returns None without raising."""
    monkeypatch.setattr("common.github._VIEWER_CACHED", False)
    monkeypatch.setattr("common.github._VIEWER_LOGIN", None)
    rec = QueuedRun(RunResult("octocat\n", "", 0))
    monkeypatch.setattr("common.github.run", rec)

    assert github.viewer_login() == "octocat"
    assert github.viewer_login() == "octocat"        # served from cache
    assert len(rec.calls) == 1                        # no second gh call
    assert rec.calls[0] == ["gh", "api", "user", "--jq", ".login"]

    # Safety: reset the cache, non-zero exit → None, no raise.
    monkeypatch.setattr("common.github._VIEWER_CACHED", False)
    monkeypatch.setattr("common.github._VIEWER_LOGIN", None)
    monkeypatch.setattr("common.github.run", QueuedRun(RunResult("", "not logged in", 1)))
    assert github.viewer_login() is None
