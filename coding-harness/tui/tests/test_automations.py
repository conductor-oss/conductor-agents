from __future__ import annotations

import json

import httpx
import pytest

from tui import api
from tui.screens.automations import build_schedule, schedule_name, validate_cron


def test_schedule_payload_defaults_and_name():
    value = build_schedule("pr_review_sweep", "https://github.com/acme/widgets.git",
                           zone_id="America/Los_Angeles")
    assert value["name"] == "conductor-pr-review-acme-widgets"
    assert value["cronExpression"] == "0 */10 * ? * *"
    assert value["runCatchupScheduleInstances"] is False
    assert value["startWorkflowRequest"]["input"] == {
        "repo": "https://github.com/acme/widgets.git", "approvalMode": "human"}
    assert validate_cron(value["cronExpression"])
    assert not validate_cron("*/10 * * * *")


def test_schedule_stores_inline_or_repo_template_with_provenance():
    inline = build_schedule(
        "pr_review_sweep", "acme/widgets", prompt_template="Review security boundaries.")
    assert inline["startWorkflowRequest"]["input"]["reviewPromptTemplate"] == (
        "Review security boundaries."
    )
    assert inline["startWorkflowRequest"]["input"]["reviewPromptTemplateSource"] == (
        "schedule:inline"
    )

    repo = build_schedule(
        "issue_resolution_sweep", "acme/widgets",
        workflow_input={"maxItems": 2, "codePromptTemplate": "old"},
        prompt_template="@.conductor/custom-code.md",
    )
    inputs = repo["startWorkflowRequest"]["input"]
    assert inputs["maxItems"] == 2
    assert inputs["codePromptTemplate"] == "@.conductor/custom-code.md"
    assert inputs["codePromptTemplateSource"] == "schedule:repo-reference"


@pytest.mark.asyncio
async def test_schedule_crud_and_auth_propagation():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("X-Authorization")))
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"token": "opaque"})
        if request.method == "GET" and request.url.path.endswith("/scheduler/schedules"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, text="")

    credentials = api.ConductorCredentials("key", "secret")
    client = api.ConductorClient("http://x/api", credentials=credentials)
    client._client = httpx.AsyncClient(base_url="http://x/api", transport=httpx.MockTransport(handler))
    await client.list_schedules()
    await client.save_schedule(build_schedule("issue_resolution_sweep", "acme/app"))
    await client.pause_schedule("s", True)
    await client.pause_schedule("s", False)
    await client.delete_schedule("s")
    assert all(token == "opaque" for method, path, token in seen if not path.endswith("/token"))
    assert ("GET", "/api/scheduler/schedules/s/pause", "opaque") in seen
    assert ("GET", "/api/scheduler/schedules/s/resume", "opaque") in seen
    await client.aclose()


@pytest.mark.asyncio
async def test_pending_approvals_paginates_excludes_timed_wait_and_keeps_nested_owner():
    calls = 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/workflow/search"):
            return httpx.Response(200, json={"totalHits": 0, "results": []})
        start = int(request.url.params.get("start", "0"))
        rows = [{"taskId": "a", "taskType": "WAIT", "status": "IN_PROGRESS",
                 "referenceTaskName": "gate", "workflowInstanceId": "child-1",
                 "workflowType": "issue_to_pr", "input": {"draft": {"title": "PR"}}},
                {"taskId": "timed", "taskType": "WAIT", "status": "IN_PROGRESS",
                 "referenceTaskName": "delay", "workflowInstanceId": "w",
                 "input": {"duration": "10m"}},
                {"taskId": "legacy", "taskType": "HUMAN", "status": "IN_PROGRESS",
                 "referenceTaskName": "old", "workflowInstanceId": "w2", "input": {}}]
        return httpx.Response(200, json={"totalHits": 3, "results": rows if start == 0 else []})
    client = api.ConductorClient("http://x/api")
    client._client = httpx.AsyncClient(base_url="http://x/api", transport=httpx.MockTransport(handler))
    items = await client.pending_approvals(page_size=100)
    assert [x.task_id for x in items] == ["a", "legacy"]
    assert items[0].workflow_id == "child-1"
    assert items[1].legacy
    await client.aclose()


@pytest.mark.asyncio
async def test_pending_approvals_falls_back_to_running_execution_for_unindexed_wait():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tasks/search"):
            return httpx.Response(200, json={"totalHits": 0, "results": []})
        if request.url.path.endswith("/workflow/search"):
            return httpx.Response(200, json={
                "totalHits": 1,
                "results": [{"workflowId": "wf-1", "workflowType": "pr_review",
                             "status": "RUNNING"}],
            })
        if request.url.path.endswith("/workflow/wf-1"):
            return httpx.Response(200, json={
                "workflowId": "wf-1",
                "workflowName": "pr_review",
                "tasks": [
                    {"taskId": "approval", "taskType": "WAIT", "status": "IN_PROGRESS",
                     "referenceTaskName": "review_gate", "workflowInstanceId": "wf-1",
                     "scheduledTime": 1000,
                     "inputData": {"workflow": "pr_review", "repo": "acme/app",
                                   "draft": {"summary": "ready"}}},
                    {"taskId": "delay", "taskType": "WAIT", "status": "IN_PROGRESS",
                     "referenceTaskName": "wait_10m", "workflowInstanceId": "wf-1",
                     "inputData": {"duration": "10m"}},
                ],
            })
        raise AssertionError(f"unexpected request: {request.url}")

    client = api.ConductorClient("http://x/api")
    client._client = httpx.AsyncClient(base_url="http://x/api",
                                       transport=httpx.MockTransport(handler))
    items = await client.pending_approvals(page_size=100)
    assert [(item.task_id, item.task_ref, item.workflow_id) for item in items] == [
        ("approval", "review_gate", "wf-1")]
    assert items[0].input["draft"]["summary"] == "ready"
    await client.aclose()
