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
from common import linked_context
from common.exec import RunError, RunResult
from gitops.tasks import pr_branch_guard, pr_checkout, pr_commit_checks, pr_create, pr_submit_review


class RecordingRun:
    """Drop-in for ``common.github.run``: records each argv (and any ``--input``
    JSON payload) and returns queued results in order."""

    def __init__(self, *results):
        self.calls: list[dict] = []
        self._results: list = list(results)

    def __call__(self, cmd, cwd=None, check=True):
        payload = None
        body_file = None
        if "--input" in cmd:
            # Capture the payload before the caller unlinks the temp file.
            payload = json.loads(Path(cmd[cmd.index("--input") + 1]).read_text())
        if "--body-file" in cmd:
            body_file = Path(cmd[cmd.index("--body-file") + 1]).read_text()
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "check": check,
                           "payload": payload, "body_file": body_file})
        result = self._results.pop(0) if self._results else RunResult("", "", 0)
        if isinstance(result, Exception):
            raise result
        return result


def _patch(monkeypatch, rec: RecordingRun) -> None:
    monkeypatch.setattr("common.github.ensure_git_auth", lambda: True)
    monkeypatch.setattr("common.github.run", rec)


# --- issue_fetch --------------------------------------------------------------

def test_issue_fetch_returns_the_raw_body_untouched_when_it_has_no_links(monkeypatch):
    meta = {"number": 57, "title": "Hello world", "body": "Just build a greeter, no links here.",
            "state": "OPEN", "url": "https://github.com/o/r/issues/57", "labels": []}
    rec = RecordingRun(RunResult(json.dumps(meta), "", 0))
    _patch(monkeypatch, rec)

    out = github.issue_fetch("o/r", 57)

    assert out["body"] == "Just build a greeter, no links here."
    assert out["linkedContext"] == ""
    assert out["linkedReferences"] == []
    assert out["linkCount"] == 0
    assert rec.calls[0]["cmd"][:4] == ["gh", "issue", "view", "57"]


def test_issue_fetch_resolves_a_linked_github_issue_reference(monkeypatch):
    # Confirmed gap: issue_fetch never resolved links inside the issue body,
    # unlike pr_comments (which resolves links found in PR feedback via the
    # same common/linked_context module). An issue can link to another issue,
    # a doc file, or a CI run just as easily as a PR comment can.
    meta = {"number": 57, "title": "Hello world",
            "body": "See background in https://github.com/o/r/issues/12 for context.",
            "state": "OPEN", "url": "https://github.com/o/r/issues/57", "labels": []}
    linked_issue = {"title": "Prior greeter discussion", "body": "We discussed defaulting to World."}
    rec = RecordingRun(
        RunResult(json.dumps(meta), "", 0),
        RunResult(json.dumps(linked_issue), "", 0),
    )
    _patch(monkeypatch, rec)

    out = github.issue_fetch("o/r", 57)

    # The raw body is never mutated by the resolved link.
    assert out["body"] == "See background in https://github.com/o/r/issues/12 for context."
    assert "## Linked context (untrusted external material)" in out["linkedContext"]
    assert "Prior greeter discussion" in out["linkedContext"]
    assert "We discussed defaulting to World." in out["linkedContext"]
    assert out["linkCount"] == 1
    assert out["linkedReferences"][0]["kind"] == "github-issue"
    assert rec.calls[1]["cmd"][:3] == ["gh", "api", "repos/o/r/issues/12"]


# --- pr_create ---------------------------------------------------------------

def test_pr_create_argv_and_parsing(fake_task_input, monkeypatch, load_fixture):
    rec = RecordingRun(RunResult(load_fixture("gh_pr_create_stdout.txt"), "", 0))
    _patch(monkeypatch, rec)

    task = fake_task_input(repoPath="/repo", title="Add retry with backoff",
                           body="Body text", base="main", head="fix/git-push-retry")
    result = pr_create(task)

    call = rec.calls[0]
    assert call["cmd"][:5] == [
        "gh", "pr", "create",
        "--title", "Add retry with backoff",
    ]
    assert call["cmd"][5] == "--body-file"
    assert call["cmd"][7:] == [
        "--base", "main",
        "--head", "fix/git-push-retry",
    ]
    assert call["body_file"] == "## Summary\n\nBody text"
    assert call["cwd"] == "/repo"
    out = result.output_data
    # Number is parsed out of the URL gh prints.
    assert out["number"] == 123
    assert out["url"].endswith("/pull/123")
    assert out["draft"] is False


def test_pr_create_empty_input_still_uses_summary_only_body(fake_task_input, monkeypatch):
    rec = RecordingRun(RunResult("https://github.com/o/n/pull/7\n", "", 0))
    _patch(monkeypatch, rec)

    # --fill is intentionally ignored: it could inject repository template sections.
    task = fake_task_input(repoPath="/repo", fill="true", draft="true")
    result = pr_create(task)

    call = rec.calls[0]
    assert call["cmd"][:5] == [
        "gh", "pr", "create", "--title", "Automated change.",
    ]
    assert call["cmd"][5] == "--body-file"
    assert call["cmd"][-1] == "--draft"
    assert call["body_file"] == "## Summary\n\nAutomated change."
    assert result.output_data["number"] == 7
    assert result.output_data["draft"] is True


def test_pr_set_draft_promotes_or_demotes_only_when_needed(monkeypatch):
    promote = RecordingRun(
        RunResult('{"isDraft": true}', "", 0),
        RunResult("", "", 0),
    )
    _patch(monkeypatch, promote)
    assert github.pr_set_draft("/repo", 7, False)["draft"] is False
    assert promote.calls[1]["cmd"] == ["gh", "pr", "ready", "7"]

    demote = RecordingRun(
        RunResult('{"isDraft": false}', "", 0),
        RunResult("", "", 0),
    )
    _patch(monkeypatch, demote)
    assert github.pr_set_draft("/repo", 8, True)["draft"] is True
    assert demote.calls[1]["cmd"] == ["gh", "pr", "ready", "8", "--undo"]


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
        "body_file": None,
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


def test_pr_submit_review_approves_clean_review(fake_task_input, monkeypatch):
    rec = RecordingRun(RunResult('{"html_url": "u"}', "", 0))
    _patch(monkeypatch, rec)

    task = fake_task_input(repo="o/n", number=1,
                           structured={"verdict": "approve", "summary": "LGTM",
                                       "comments": []})
    result = pr_submit_review(task)

    assert rec.calls[0]["payload"] == {
        "body": "LGTM", "event": "APPROVE", "comments": [],
    }
    assert result.output_data["event"] == "APPROVE"


def test_pr_submit_review_accepts_json_string_structured(fake_task_input, monkeypatch):
    rec = RecordingRun(RunResult('{"html_url": "u"}', "", 0))
    _patch(monkeypatch, rec)

    structured = json.dumps({"verdict": "approve", "summary": "LGTM", "comments": []})
    task = fake_task_input(repo="o/n", number=2, structured=structured)
    result = pr_submit_review(task)

    assert rec.calls[0]["payload"]["event"] == "APPROVE"
    assert result.output_data["inlineCount"] == 0


def test_pr_submit_review_human_event_overrides_agent_verdict(fake_task_input, monkeypatch):
    rec = RecordingRun(RunResult('{"html_url": "u"}', "", 0))
    _patch(monkeypatch, rec)

    task = fake_task_input(
        repo="o/n", number=2, event="REQUEST_CHANGES",
        structured={"verdict": "approve", "summary": "Please add the missing test.",
                    "comments": []},
    )
    result = pr_submit_review(task)

    assert rec.calls[0]["payload"]["event"] == "REQUEST_CHANGES"
    assert rec.calls[0]["payload"]["body"] == "Please add the missing test."
    assert result.output_data["event"] == "REQUEST_CHANGES"


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


def test_pr_comments_appends_authenticated_actions_failure_context(monkeypatch):
    """A CI link in actionable feedback contributes the failed compiler log tail."""
    meta = {
        "number": 9, "title": "Fix MCP", "headRefName": "fix", "baseRefName": "main",
        "url": "https://github.com/o/r/pull/9", "headRepositoryOwner": {"login": "o"},
        "headRepository": {"name": "r"},
    }
    action_meta = {"workflowName": "build", "conclusion": "failure",
                   "jobs": [{"databaseId": 77, "name": "compile", "conclusion": "failure"}]}
    rec = RecordingRun(
        RunResult(json.dumps(meta), "", 0),
        RunResult(json.dumps([[{"user": {"login": "reviewer"},
                               "body": "Please fix CI https://github.com/o/r/actions/runs/123"}]]), "", 0),
        RunResult("[[]]", "", 0), RunResult("[[]]", "", 0),
        RunResult(json.dumps(action_meta), "", 0),
        RunResult("compile output\nerror: duplicate @ConfigurationProperties for mcp.process.pool in McpBootAutoConfiguration.java\n", "", 0),
    )
    _patch(monkeypatch, rec)

    out = github.pr_comments("o/r", 9)

    assert "## Conversation comments" in out["feedback"]
    assert "## Linked context (untrusted external material)" in out["feedback"]
    assert "duplicate @ConfigurationProperties" in out["feedback"]
    assert out["linkCount"] == 1
    assert out["linkedReferences"][0]["kind"] == "github-actions"
    assert rec.calls[1]["cmd"][-2:] == ["--paginate", "--slurp"]
    assert rec.calls[4]["cmd"][:6] == ["gh", "run", "view", "123", "--repo", "o/r"]
    assert rec.calls[5]["cmd"][-3:] == ["--job", "77", "--log-failed"]


def test_pr_comments_scopes_actions_job_url_to_requested_job(monkeypatch):
    meta = {
        "number": 9, "title": "Fix MCP", "headRefName": "fix", "baseRefName": "main",
        "url": "https://github.com/o/r/pull/9", "headRepositoryOwner": {"login": "o"},
        "headRepository": {"name": "r"},
    }
    action_meta = {
        "workflowName": "CI", "conclusion": "failure",
        "jobs": [
            {"databaseId": 92806090468, "name": "build", "conclusion": "failure"},
            {"databaseId": 92806653120, "name": "Unit Test results", "conclusion": "failure"},
        ],
    }
    job_url = "https://github.com/o/r/actions/runs/31159405218/job/92806090468"
    rec = RecordingRun(
        RunResult(json.dumps(meta), "", 0),
        RunResult(json.dumps([[{"user": {"login": "reviewer"},
                               "body": f"Fix {job_url}"}]]), "", 0),
        RunResult("[[]]", "", 0), RunResult("[[]]", "", 0),
        RunResult(json.dumps(action_meta), "", 0),
        RunResult("spotlessJavaCheck FAILED\n", "", 0),
    )
    _patch(monkeypatch, rec)

    out = github.pr_comments("o/r", 9)

    assert out["linkCount"] == 1
    assert "Matched job: build" in out["feedback"]
    log_calls = [call["cmd"] for call in rec.calls if "--log-failed" in call["cmd"]]
    assert log_calls == [[
        "gh", "run", "view", "31159405218", "--repo", "o/r",
        "--job", "92806090468", "--log-failed",
    ]]


def test_run_link_keeps_available_logs_when_generated_check_has_no_archive():
    class API:
        @staticmethod
        def run_view(_repo, _run_id):
            return {
                "workflowName": "CI", "conclusion": "failure",
                "jobs": [
                    {"databaseId": 10, "name": "build", "conclusion": "failure"},
                    {"databaseId": 11, "name": "Unit Test results", "conclusion": "failure"},
                ],
            }

        @staticmethod
        def run_failed_logs(_repo, _run_id, *, job_id=None):
            if job_id == "11":
                raise RunError("gh run view", 1, "", "log not found: 11")
            return "build error"

    refs, warnings, _used = linked_context.resolve(
        ["https://github.com/o/r/actions/runs/123"], API,
    )

    assert warnings == []
    assert len(refs) == 1
    assert "build error" in refs[0]["content"]
    assert "Unit Test results: gh run view exited 1" in refs[0]["content"]


def test_pr_status_distinguishes_failed_checks_from_cli_failure(monkeypatch):
    failed_checks = RecordingRun(
        RunResult('{"number": 7, "state": "OPEN"}', "", 0),
        RunResult('[{"name": "unit", "bucket": "fail", "state": "FAILURE"}]', "", 1),
    )
    _patch(monkeypatch, failed_checks)
    assert github.pr_status("/repo", 7)["failing"] == 1

    cli_failure = RecordingRun(
        RunResult('{"number": 7, "state": "OPEN"}', "", 0),
        RunResult("", "authentication failed", 1),
    )
    _patch(monkeypatch, cli_failure)
    try:
        github.pr_status("/repo", 7)
    except RuntimeError as exc:
        assert "authentication failed" in str(exc)
    else:
        raise AssertionError("gh pr checks CLI failure was treated as an empty check list")


def test_pr_diff_uses_paginated_rest_files(monkeypatch):
    rec = RecordingRun(
        RunResult('{"base": {"ref": "main"}}', "", 0),
        RunResult('[[{"filename": "a.py"}], [{"filename": "b.py"}]]', "", 0),
        RunResult("diff --git a/a.py b/a.py\n", "", 0),
    )
    _patch(monkeypatch, rec)

    out = github.pr_diff("o/r", 12)

    assert out["changedFiles"] == ["a.py", "b.py"]
    assert rec.calls[1]["cmd"] == [
        "gh", "api", "repos/o/r/pulls/12/files?per_page=100", "--paginate", "--slurp",
    ]


def test_commit_checks_requires_nonempty_explicit_success(monkeypatch):
    responses = [
        {"check_runs": [{"name": "unit", "status": "completed", "conclusion": "success", "html_url": "https://ci/unit"}]},
        {"statuses": [{"context": "legacy", "state": "success", "target_url": "https://ci/legacy"}]},
    ]
    monkeypatch.setattr("common.github.api_json_retry", lambda *args, **kwargs: responses.pop(0))

    outcome = github.commit_checks("o/r", "a" * 40)

    assert outcome["verificationState"] == "passed"
    assert outcome["checkCount"] == 2


def test_commit_checks_blocks_empty_skipped_and_pending(monkeypatch):
    cases = [
        ({"check_runs": []}, {"statuses": []}, "empty"),
        ({"check_runs": [{"name": "lint", "status": "completed", "conclusion": "skipped"}]}, {"statuses": []}, "unknown"),
        ({"check_runs": [{"name": "test", "status": "in_progress", "conclusion": None}]}, {"statuses": []}, "pending"),
    ]
    for checks, statuses, expected in cases:
        values = [checks, statuses]
        monkeypatch.setattr("common.github.api_json_retry", lambda *args, **kwargs: values.pop(0))
        assert github.commit_checks("o/r", "b" * 40)["verificationState"] == expected


def test_commit_checks_aggregates_all_pages_before_allowing_success(monkeypatch):
    responses = [
        [
            {"check_runs": [{"name": "unit", "status": "completed", "conclusion": "success"}]},
            {"check_runs": [{"name": "late-failure", "status": "completed", "conclusion": "failure"}]},
        ],
        [{"statuses": [{"context": "legacy", "state": "success"}]}],
    ]
    calls = []

    def paged(path, **kwargs):
        calls.append((path, kwargs))
        return responses.pop(0)

    monkeypatch.setattr("common.github.api_json_retry", paged)
    result = github.commit_checks("o/r", "c" * 40)

    assert result["verificationState"] == "failed"
    assert result["checkCount"] == 3
    assert all(kwargs["paginate"] is True for _, kwargs in calls)


def test_commit_checks_accepts_paginated_bare_statuses_response(monkeypatch):
    responses = [
        [{"check_runs": [{"name": "unit", "status": "completed", "conclusion": "success"}]}],
        [{"context": "legacy", "state": "failure"}],
    ]
    monkeypatch.setattr("common.github.api_json_retry", lambda *args, **kwargs: responses.pop(0))

    result = github.commit_checks("o/r", "d" * 40)

    assert result["verificationState"] == "failed"
    assert result["checkCount"] == 2


def test_pr_commit_checks_converts_api_failure_to_unknown_evidence(fake_task_input, monkeypatch):
    monkeypatch.setattr("common.github.commit_checks", lambda *_args: (_ for _ in ()).throw(RuntimeError("rate limited")))

    result = pr_commit_checks(fake_task_input(repo="o/r", sha="a" * 40))

    assert result.status.value == "COMPLETED"
    assert result.output_data["verificationState"] == "unknown"
    assert "rate limited" in result.output_data["reason"]


def test_pr_branch_guard_converts_api_failure_to_unknown_evidence(fake_task_input, monkeypatch):
    monkeypatch.setattr("common.github.remote_branch_head", lambda *_args: (_ for _ in ()).throw(RuntimeError("unavailable")))

    result = pr_branch_guard(fake_task_input(repo="o/r", branch="fix", expectedHeadSha="b" * 40))

    assert result.status.value == "COMPLETED"
    assert result.output_data["verificationState"] == "unknown"
    assert result.output_data["actualHeadSha"] == ""


def test_link_extraction_redacts_credentials_and_caps_deduplicates():
    urls, warnings = linked_context.extract_urls([
        "https://example.com/a?utm_source=x https://example.com/a",
        "https://example.com/s?token=secret http://example.com/no",
    ])
    assert urls == ["https://example.com/a"]
    assert any("credential-bearing" in warning and "secret" not in warning for warning in warnings)
    assert any("non-HTTPS" in warning for warning in warnings)
