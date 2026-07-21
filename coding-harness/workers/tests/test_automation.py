from __future__ import annotations

import json
from pathlib import Path

from automation.tasks import github_automation_claim, github_automation_scan
from common import automation


TASKDEFS = Path(__file__).resolve().parents[1] / "workflows" / "taskdefs"
AUTOMATION_PATH_TASKS = {
    "campaign_checks", "coding_agent", "commit", "git_push",
    "github_automation_claim", "github_automation_scan", "github_automation_state",
    "local_diff", "merge_worktrees", "pr_comment", "pr_comments", "pr_diff",
    "pr_submit_review", "workspace_cleanup", "workspace_prepare", "worktree_add",
}


def _comment(body, author="conductor-bot", ident=1):
    return {"id": ident, "body": body, "user": {"login": author}}


def test_automation_path_taskdefs_have_all_three_timeouts():
    for name in sorted(AUTOMATION_PATH_TASKS):
        taskdef = json.loads((TASKDEFS / f"{name}.json").read_text())
        for field in ("timeoutSeconds", "responseTimeoutSeconds", "pollTimeoutSeconds"):
            assert int(taskdef.get(field) or 0) > 0, f"{name}: {field} must be positive"


def test_automation_dispatch_carries_bounded_approval_configuration():
    workflows = Path(__file__).resolve().parents[1] / "workflows"
    workflow = json.loads((workflows / "automation_dispatch.json").read_text())
    serialized = json.dumps(workflow)
    assert '"maxApprovalRevisions"' in serialized
    assert '"approvalJudgePromptTemplate"' in serialized
    assert '"reviewPromptTemplate"' in serialized
    assert '"fixPromptTemplate"' in serialized
    assert '"modelProfile"' in serialized


def test_markers_require_trusted_author_and_version():
    body = automation.encode_marker({"kind": "review", "revision": "abc", "status": "claimed", "attempt": 1})
    assert automation.parse_marker(body, author="conductor-bot", trusted_author="conductor-bot")
    assert not automation.parse_marker(body, author="attacker", trusted_author="conductor-bot")
    assert not automation.parse_marker("<!-- conductor-automation:{} -->", author="conductor-bot", trusted_author="conductor-bot")


def test_sha_revision_dedup_and_retry_policy():
    body = automation.encode_marker({"kind": "review", "revision": "sha-a", "status": "started",
                                     "attempt": 1, "workflowId": "w1", "timestamp": "2026-01-01T00:00:00Z"})
    marker = automation.trusted_markers([_comment(body)], "conductor-bot")
    assert automation.revision_decision(marker, "sha-a", now_epoch=2_000_000_000,
                                        workflow_status={"w1": "COMPLETED"})[0] is False
    assert automation.revision_decision(marker, "sha-b", now_epoch=2_000_000_000,
                                        workflow_status={"w1": "COMPLETED"}) == (True, "new", 1)
    discovered = automation.revision_decision(marker, "sha-a", now_epoch=2_000_000_000,
                                              workflow_status={"w1": "FAILED"})
    assert discovered == (False, "record_failure", 1)
    failed = automation.Marker("review", "sha-a", "failed", 1,
                               workflow_id="w1", timestamp="2026-01-01T00:10:00Z",
                               comment_id=2)
    assert automation.revision_decision([*marker, failed], "sha-a",
                                        now_epoch=1767226500,
                                        workflow_status={"w1": "FAILED"}) == \
        (False, "retry_backoff", 1)
    assert automation.revision_decision([*marker, failed], "sha-a",
                                        now_epoch=1767228001,
                                        workflow_status={"w1": "FAILED"}) == \
        (True, "retry", 2)


def test_active_hitl_claim_never_expires_and_stop_is_suppressed():
    body = automation.encode_marker({"kind": "issue", "revision": "r", "status": "started",
                                     "attempt": 1, "workflowId": "wait", "timestamp": "2020-01-01T00:00:00Z"})
    markers = automation.trusted_markers([_comment(body)], "conductor-bot")
    assert automation.revision_decision(markers, "r", now_epoch=4_000_000_000,
                                        workflow_status={"wait": "RUNNING"})[:2] == (False, "active")
    assert automation.revision_decision(markers, "r", now_epoch=4_000_000_000,
                                        workflow_status={"wait": "SUPPRESSED"})[:2] == (False, "suppressed")


def test_feedback_fingerprint_is_normalized_and_excludes_markers():
    a = [{"id": 1, "updated_at": "x", "body": " fix   this ", "user": {"login": "alice"}}]
    b = [{"id": 99, "body": automation.encode_marker({"kind": "address", "revision": "x", "status": "claimed"}),
          "user": {"login": "bot"}}]
    assert automation.feedback_fingerprint(a, b) == automation.feedback_fingerprint(
        [{"id": 1, "updated_at": "x", "body": "fix this", "user": {"login": "alice"}}])
    assert automation.feedback_fingerprint(a) != automation.feedback_fingerprint(
        [{"id": 1, "updated_at": "y", "body": "fix this", "user": {"login": "alice"}}])
    operational = [{"id": 2, "body": "done <!-- conductor-harness -->",
                    "user": {"login": "bot"}}]
    assert automation.actionable_feedback_count(operational, excluded_author="bot") == 0
    assert automation.actionable_feedback_count(a, operational, excluded_author="bot") == 1


def test_issue_filter_and_linked_pr_detection():
    issue = {"state": "OPEN", "labels": [{"name": "conductor:auto"}]}
    assert automation.issue_has_label(issue)
    assert not automation.issue_has_label({**issue, "isDraft": True})
    assert automation.linked_pr_exists(12, [{"state": "OPEN", "body": "Closes #12"}])
    assert automation.linked_pr_exists(12, [{"state": "MERGED", "headRefName": "harness/issue-12"}])
    assert not automation.linked_pr_exists(12, [{"state": "CLOSED", "body": "Closes #12"}])
    before = automation.issue_revision({**issue, "id": 1, "title": "x", "body": "y", "updated_at": "a"})
    after = automation.issue_revision({**issue, "id": 1, "title": "x", "body": "y", "updated_at": "b"})
    assert before == after


def test_reset_allows_explicit_reclaim():
    marker = automation.Marker("review", "sha", "reset", 1)
    assert automation.revision_decision([marker], "sha", now_epoch=0) == (True, "reset", 1)


def test_newer_reset_overrides_exhausted_higher_attempt():
    exhausted = automation.Marker("review", "sha", "exhausted", 3, comment_id=10)
    reset = automation.Marker("review", "sha", "reset", 1, comment_id=11)
    assert automation.revision_decision([exhausted, reset], "sha", now_epoch=0) == \
        (True, "reset", 1)


def test_claim_race_tracks_earliest_claim_dispatch_status():
    winner = automation.Marker("review", "sha", "claimed", 1,
                               workflow_id="dispatch-winner", comment_id=10)
    loser = automation.Marker("review", "sha", "claimed", 1,
                              workflow_id="dispatch-loser", comment_id=11)
    assert automation.revision_decision(
        [winner, loser], "sha", now_epoch=0,
        workflow_status={"dispatch-winner": "RUNNING", "dispatch-loser": "COMPLETED"}) == \
        (False, "active", 1)
    assert automation.revision_decision(
        [winner, loser], "sha", now_epoch=0,
        workflow_status={"dispatch-winner": "FAILED", "dispatch-loser": "COMPLETED"}) == \
        (False, "record_failure", 1)


def test_mocked_review_address_rereview_loop_deduplicates_each_revision():
    now = 2_000_000_000
    assert automation.revision_decision([], "sha-a", now_epoch=now) == (True, "new", 1)
    review_a = automation.Marker("review", "sha-a", "completed", 1, comment_id=1)
    assert automation.revision_decision([review_a], "sha-a", now_epoch=now)[:2] == \
        (False, "completed")

    feedback_a = automation.feedback_fingerprint([
        {"id": 10, "updated_at": "1", "body": "Fix the race", "user": {"login": "reviewer"}}])
    assert automation.revision_decision([], feedback_a, now_epoch=now) == (True, "new", 1)
    addressed_a = automation.Marker("address", feedback_a, "completed", 1, comment_id=2)
    assert automation.revision_decision([addressed_a], feedback_a, now_epoch=now)[:2] == \
        (False, "completed")

    feedback_b = automation.feedback_fingerprint([
        {"id": 10, "updated_at": "2", "body": "Fix the race and add a test",
         "user": {"login": "reviewer"}}])
    assert feedback_b != feedback_a
    assert automation.revision_decision([addressed_a], feedback_b, now_epoch=now) == \
        (True, "new", 1)
    assert automation.revision_decision([review_a], "sha-b", now_epoch=now) == \
        (True, "new", 1)


def test_claim_marker_tracks_dispatch_execution(fake_task_input, monkeypatch):
    posted = {}

    monkeypatch.setattr("automation.tasks.github.authenticated_login", lambda: "bot")

    def post(_repo, _number, body):
        posted["body"] = body
        return {"id": 10}

    monkeypatch.setattr("automation.tasks.github.post_issue_comment", post)
    monkeypatch.setattr("automation.tasks.github.issue_comments", lambda *_: [
        _comment(posted["body"], author="bot", ident=10)])
    result = github_automation_claim(fake_task_input(
        repo="acme/app", number=7, kind="review", revision="sha", attempt=1,
        claimId="dispatch-123"))
    marker = automation.parse_marker(posted["body"], author="bot", trusted_author="bot")
    assert result.output_data["claimed"] is True
    assert marker and marker.workflow_id == "dispatch-123"


def test_scan_records_failure_before_retry_backoff(fake_task_input, monkeypatch):
    started = automation.encode_marker({
        "kind": "review", "revision": "sha", "status": "started", "attempt": 1,
        "workflowId": "child-1", "timestamp": "2020-01-01T00:00:00Z"})
    comments = [_comment(started, author="bot", ident=1)]
    posted = []
    monkeypatch.setattr("automation.tasks.github.authenticated_login", lambda: "bot")
    monkeypatch.setattr("automation.tasks.github.list_open_pulls", lambda _repo: [
        {"number": 7, "title": "PR", "draft": False, "head": {"sha": "sha"}}])
    monkeypatch.setattr("automation.tasks.github.issue_comments", lambda *_: comments)
    monkeypatch.setattr("automation.tasks._workflow_statuses", lambda _ids: {"child-1": "FAILED"})
    monkeypatch.setattr("automation.tasks.github.post_issue_comment",
                        lambda _r, _n, body: posted.append(body) or {"id": 2})
    result = github_automation_scan(fake_task_input(
        kind="review", repo="acme/app", maxNew=5, maxActive=5))
    assert result.output_data["eligible"] == 0
    assert result.output_data["dynamicTasks"] == []
    assert result.output_data["skipped"][0]["reason"] == "retry_backoff"
    marker = automation.parse_marker(posted[0], author="bot", trusted_author="bot")
    assert marker and marker.status == "failed" and marker.attempt == 1


def test_address_scan_ignores_only_operational_feedback(fake_task_input, monkeypatch):
    pr = {"number": 9, "title": "Generated PR", "draft": False,
          "body": "<!-- conductor-origin:issue_to_pr -->", "head": {"sha": "sha"}}
    operational = {"id": 3, "body": "Addressed. <!-- conductor-harness -->",
                   "user": {"login": "bot"}}
    monkeypatch.setattr("automation.tasks.github.authenticated_login", lambda: "bot")
    monkeypatch.setattr("automation.tasks.github.list_open_pulls", lambda _repo: [pr])
    monkeypatch.setattr("automation.tasks.github.api_json", lambda *_a, **_k: [])
    monkeypatch.setattr("automation.tasks.github.issue_comments", lambda *_: [operational])
    result = github_automation_scan(fake_task_input(
        kind="address", repo="acme/app", maxNew=2, maxActive=2))
    assert result.output_data["eligible"] == 0
    assert result.output_data["skipped"] == [{"number": 9, "reason": "no_feedback"}]
