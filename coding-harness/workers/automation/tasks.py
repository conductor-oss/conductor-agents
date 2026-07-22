"""Workers backing the schedulable GitHub automation sweeps."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from time import time

from conductor.client.worker.worker_task import worker_task

from common import automation, github
from common.results import fail, ok


_CLAIM_LOCK = threading.Lock()


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _conductor_token(base: str) -> str:
    token = os.environ.get("CONDUCTOR_AUTH_TOKEN", "")
    key = os.environ.get("CONDUCTOR_AUTH_KEY", "")
    secret = os.environ.get("CONDUCTOR_AUTH_SECRET", "")
    if token or not (key and secret):
        return token
    req = urllib.request.Request(
        f"{base.rstrip('/')}/token",
        data=json.dumps({"keyId": key, "keySecret": secret}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:  # noqa: S310
            return str(json.loads(response.read() or b"{}").get("token") or "")
    except (OSError, ValueError):
        return ""


def _workflow_statuses(ids: set[str]) -> dict[str, str]:
    base = os.environ.get("CONDUCTOR_SERVER_URL", "").rstrip("/")
    if not base or not ids:
        return {}
    token = _conductor_token(base)
    statuses = {}
    for workflow_id in ids:
        req = urllib.request.Request(f"{base}/workflow/{workflow_id}?includeTasks=true")
        if token:
            req.add_header("X-Authorization", token)
        try:
            with urllib.request.urlopen(req, timeout=4) as response:  # noqa: S310
                execution = json.loads(response.read())
                suppressed = any(bool((item.get("outputData") or {}).get("suppressed"))
                                 for item in execution.get("tasks") or [])
                statuses[workflow_id] = "SUPPRESSED" if suppressed else str(execution.get("status") or "")
        except (OSError, ValueError):
            continue
    return statuses


def _feedback(repo: str, number: int, login: str) -> tuple[str, int]:
    slug = github.repo_slug(repo)
    reviews = github.api_json(f"repos/{slug}/pulls/{number}/reviews?per_page=100", paginate=True)
    inline = github.api_json(f"repos/{slug}/pulls/{number}/comments?per_page=100", paginate=True)
    conversation = github.issue_comments(repo, number)
    return automation.feedback_fingerprint(reviews, inline, conversation,
                                           excluded_author=login), \
        automation.actionable_feedback_count(reviews, inline, conversation,
                                             excluded_author=login)


def _candidate(kind: str, number: int, revision: str, attempt: int, source: dict) -> dict:
    return {
        "kind": kind,
        "childWorkflow": {"review": "pr_review", "address": "address_pr", "issue": "issue_to_pr"}[kind],
        "number": number,
        "revision": revision,
        "attempt": attempt,
        "title": source.get("title") or "",
        "url": source.get("html_url") or source.get("url") or "",
    }


@worker_task(task_definition_name="github_automation_scan")
def github_automation_scan(task):
    i = task.input_data or {}
    kind = str(i.get("kind") or "").strip()
    repo = str(i.get("repo") or "").strip()
    limit = _int(i.get("maxNew"), {"review": 5, "address": 2, "issue": 1}.get(kind, 1))
    active_limit = _int(i.get("maxActive"), limit)
    try:
        login = github.authenticated_login()
        pulls = github.list_open_pulls(repo)
        sources = github.list_open_issues(repo) if kind == "issue" else pulls
        prepared: list[tuple[dict, str, list[automation.Marker]]] = []
        skipped: list[dict] = []
        all_ids: set[str] = set()

        if kind == "issue":
            all_prs = github.api_json(
                f"repos/{github.repo_slug(repo)}/pulls?state=all&per_page=100", paginate=True)
            label = str(i.get("issueLabel") or "conductor:auto")
            for issue in sources:
                if not automation.issue_has_label(issue, label):
                    skipped.append({"number": issue.get("number"), "reason": "label_or_state"})
                    continue
                if automation.linked_pr_exists(int(issue["number"]), all_prs):
                    skipped.append({"number": issue["number"], "reason": "linked_pr"})
                    continue
                revision = automation.issue_revision(issue)
                markers = [m for m in automation.trusted_markers(
                    github.issue_comments(repo, issue["number"]), login) if m.kind == kind]
                prepared.append((issue, revision, markers))
                all_ids.update(m.workflow_id for m in markers if m.workflow_id)
        else:
            for pr in sources:
                if pr.get("draft"):
                    skipped.append({"number": pr.get("number"), "reason": "draft"})
                    continue
                if kind == "address" and "<!-- conductor-origin:issue_to_pr -->" not in str(pr.get("body") or ""):
                    skipped.append({"number": pr.get("number"), "reason": "not_harness_created"})
                    continue
                if kind == "review":
                    revision = str((pr.get("head") or {}).get("sha") or "")
                else:
                    revision, count = _feedback(repo, int(pr["number"]), login)
                    if not count:
                        skipped.append({"number": pr["number"], "reason": "no_feedback"})
                        continue
                markers = [m for m in automation.trusted_markers(
                    github.issue_comments(repo, pr["number"]), login) if m.kind == kind]
                prepared.append((pr, revision, markers))
                all_ids.update(m.workflow_id for m in markers if m.workflow_id)

        statuses = _workflow_statuses(all_ids)
        active = sum(1 for status in statuses.values() if status in automation.ACTIVE_STATUSES)
        capacity = max(0, min(limit, active_limit - active))
        eligible, blocked = [], []
        for source, revision, markers in prepared:
            allowed, reason, attempt = automation.revision_decision(
                markers, revision, now_epoch=time(), workflow_status=statuses)
            if reason == "record_failure":
                latest = max((m for m in markers if m.revision == revision),
                             key=lambda m: (m.comment_id, m.attempt), default=None)
                failed_attempt = latest.attempt if latest else max(1, attempt)
                if not any(m.revision == revision and m.status == "failed" and
                           m.attempt == failed_attempt for m in markers):
                    github.post_issue_comment(repo, int(source["number"]), automation.encode_marker({
                        "kind": kind, "revision": revision, "status": "failed",
                        "attempt": failed_attempt,
                        "workflowId": latest.workflow_id if latest else "",
                        "timestamp": automation.utc_now(),
                    }))
                reason = "retry_backoff"
            item = _candidate(kind, int(source["number"]), revision, attempt, source)
            if allowed and len(eligible) < capacity:
                eligible.append(item)
            elif allowed:
                blocked.append({**item, "reason": "capacity"})
            else:
                if reason == "suppressed" and not any(
                        m.revision == revision and m.status == "suppressed" for m in markers):
                    latest = max((m for m in markers if m.revision == revision),
                                 key=lambda m: (m.attempt, m.comment_id), default=None)
                    github.post_issue_comment(repo, int(source["number"]), automation.encode_marker({
                        "kind": kind, "revision": revision, "status": "suppressed",
                        "attempt": latest.attempt if latest else attempt,
                        "workflowId": latest.workflow_id if latest else "",
                        "timestamp": automation.utc_now(),
                    }))
                if reason == "exhausted" and not any(
                        m.revision == revision and m.status == "exhausted" for m in markers):
                    latest = max((m for m in markers if m.revision == revision),
                                 key=lambda m: (m.attempt, m.comment_id), default=None)
                    github.post_issue_comment(repo, int(source["number"]), automation.encode_marker({
                        "kind": kind, "revision": revision, "status": "exhausted",
                        "attempt": latest.attempt if latest else attempt,
                        "workflowId": latest.workflow_id if latest else "",
                        "timestamp": automation.utc_now(),
                    }))
                skipped.append({**item, "reason": reason})

        dynamic_tasks, dynamic_inputs = [], {}
        for index, item in enumerate(eligible):
            ref = f"dispatch_{kind}_{item['number']}_{index}"
            dynamic_tasks.append({
                "name": "automation_dispatch", "taskReferenceName": ref,
                "type": "SUB_WORKFLOW", "subWorkflowParam": {"name": "automation_dispatch", "version": 1},
            })
            dynamic_inputs[ref] = {**i, **item, "trustedAuthor": login}
        output = {
            "scanned": len(sources), "eligible": len(eligible), "active": active,
            "candidates": eligible, "skipped": skipped, "blocked": blocked,
            "dynamicTasks": dynamic_tasks, "dynamicTasksInput": dynamic_inputs,
        }
        return ok(task, output, [f"[github_automation_scan] {kind} scanned={len(sources)} eligible={len(eligible)} active={active}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "github_automation_scan", exc)


@worker_task(task_definition_name="github_automation_claim")
def github_automation_claim(task):
    i = task.input_data or {}
    try:
        repo, number = str(i["repo"]), int(i["number"])
        login = github.authenticated_login()
        marker_data = {
            "kind": str(i["kind"]), "revision": str(i["revision"]),
            "status": "claimed", "attempt": _int(i.get("attempt"), 1),
            "timestamp": automation.utc_now(),
            "claimId": str(i.get("claimId") or getattr(task, "workflow_instance_id", "")),
        }
        marker_data["workflowId"] = marker_data["claimId"]
        with _CLAIM_LOCK:
            posted = github.post_issue_comment(repo, number, automation.encode_marker(marker_data))
            markers = automation.trusted_markers(github.issue_comments(repo, number), login)
            competing = [m for m in markers if m.kind == marker_data["kind"] and
                         m.revision == marker_data["revision"] and
                         m.attempt == marker_data["attempt"] and m.status == "claimed"]
            winner = min(competing, key=lambda m: m.comment_id) if competing else None
        won = bool(winner and winner.comment_id == int(posted.get("id") or 0))
        return ok(task, {"claimed": won, "claimCommentId": posted.get("id"),
                         "trustedAuthor": login, "reason": "won" if won else "race_lost"},
                  [f"[github_automation_claim] #{number} {'won' if won else 'lost'}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "github_automation_claim", exc)


@worker_task(task_definition_name="github_automation_state")
def github_automation_state(task):
    i = task.input_data or {}
    try:
        data = {
            "kind": str(i["kind"]), "revision": str(i["revision"]),
            "status": str(i.get("status") or "completed").lower(),
            "attempt": _int(i.get("attempt"), 1), "timestamp": automation.utc_now(),
            "workflowId": str(i.get("workflowId") or ""),
            "outcome": i.get("outcome") or {},
        }
        comment = github.post_issue_comment(str(i["repo"]), int(i["number"]),
                                            automation.encode_marker(data))
        return ok(task, {"recorded": True, "commentId": comment.get("id"), **data},
                  [f"[github_automation_state] #{i['number']} {data['status']}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "github_automation_state", exc)
