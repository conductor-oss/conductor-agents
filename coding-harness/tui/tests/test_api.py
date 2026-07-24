"""api.py parsing / model / client tests — no live network.

Uses captured execution fixtures for parsing + snapshot/aggregation, and
httpx.MockTransport for the client mutation methods.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from tui import api
from tui.auth import AuthConfigurationError

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _clear_conductor_auth(monkeypatch):
    """Keep unauthenticated client tests independent of the developer environment."""
    monkeypatch.delenv("CONDUCTOR_AUTH_KEY", raising=False)
    monkeypatch.delenv("CONDUCTOR_AUTH_SECRET", raising=False)


def _fix(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def test_parse_execution_pr_review():
    run, tasks = api.parse_execution(_fix("pr_review_completed"))
    assert run.workflow == "pr_review"
    assert run.status in api.TERMINAL
    assert not run.running
    # a coding_agent task exists and yields a snapshot
    agents = [t for t in tasks if t.is_coding_agent]
    assert agents, "expected a coding_agent task in pr_review"
    snap = agents[0].snapshot()
    assert snap and snap.tokens > 0 and snap.num_turns >= 1


def test_parse_execution_has_subworkflow_node():
    run, tasks = api.parse_execution(_fix("issue_to_pr_running"))
    subs = [t for t in tasks if t.type == "SUB_WORKFLOW"]
    assert subs, "expected a SUB_WORKFLOW task (code_parallel) in issue_to_pr"
    assert subs[0].sub_workflow_id, "SUB_WORKFLOW task must carry subWorkflowId for recursion"
    assert all(t.workflow_id == run.id for t in tasks)


def test_tokens_cost_prefers_terminal_totals():
    run, tasks = api.parse_execution(_fix("pr_review_completed"))
    detail = api.RunDetail(run=run, tasks=tasks)
    tokens, cost = detail.tokens_cost()
    assert tokens > 0
    # terminal pr_review reports tokenUsed/costUsd at the top level
    assert tokens == int(run.output.get("tokenUsed") or 0)


def test_tokens_cost_aggregates_when_running():
    # synthetic running tree: two coding_agent forks under a sub-workflow
    def agent_node(tokens, cost, turns):
        return api.TaskNode(ref="code", def_name="coding_agent", type="SIMPLE",
                            status="IN_PROGRESS", task_id="x",
                            output={"tokenUsed": tokens, "costUsd": cost, "numTurns": turns,
                                    "running": True, "turns": []})
    sub = api.TaskNode(ref="cp", def_name="", type="SUB_WORKFLOW", status="IN_PROGRESS",
                       task_id="s", output={}, sub_workflow_id="sub1",
                       children=[agent_node(1000, 0.5, 3), agent_node(500, 0.25, 2)])
    run = api.Run(id="w", workflow="issue_to_pr", status="RUNNING", start_ms=0, end_ms=None)
    detail = api.RunDetail(run=run, tasks=[sub])
    tokens, cost = detail.tokens_cost()
    assert tokens == 1500
    assert abs(cost - 0.75) < 1e-9
    assert len(detail.coding_agents()) == 2


def test_to_ms_parses_iso_and_epoch():
    assert api._to_ms(1783414411776) == 1783414411776
    assert api._to_ms("1783414411776") == 1783414411776
    ms = api._to_ms("2026-07-07T21:52:48.414Z")
    assert isinstance(ms, int) and ms > 1_700_000_000_000


def test_interactive_workflow_contract_requires_wait_checkpoints():
    stale = {
        "tasks": [{
            "type": "SWITCH",
            "decisionCases": {
                "true": [{"taskReferenceName": "review_gate", "type": "HUMAN"}],
            },
        }],
    }
    current = {
        "tasks": [{
            "type": "SWITCH",
            "decisionCases": {
                "true": [{"taskReferenceName": "review_gate", "type": "WAIT"}],
            },
        }],
    }
    assert not api._workflow_contract_current("pr_review", stale)
    assert api._workflow_contract_current("pr_review", current)
    assert api._workflow_contract_current("code_parallel", stale)


@pytest.mark.asyncio
async def test_workflow_registered_rejects_stale_signal_contract():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/metadata/workflow/pr_review"
        return httpx.Response(200, json={
            "name": "pr_review",
            "tasks": [{"taskReferenceName": "review_gate", "type": "HUMAN"}],
        })

    client = api.ConductorClient("http://x/api")
    client._client = httpx.AsyncClient(base_url="http://x/api",
                                       transport=httpx.MockTransport(handler))
    assert not await client.workflow_registered("pr_review")
    await client.aclose()


@pytest.mark.asyncio
async def test_client_start_returns_plain_text_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/workflow/pr_review"
        assert json.loads(request.content) == {"repo": "acme/app", "prNumber": 7}
        return httpx.Response(200, text="wf-abc-123")

    client = api.ConductorClient("http://x/api")
    client._client = httpx.AsyncClient(base_url="http://x/api",
                                       transport=httpx.MockTransport(handler))
    wid = await client.start("pr_review", {"repo": "acme/app", "prNumber": 7})
    assert wid == "wf-abc-123"
    await client.aclose()


@pytest.mark.asyncio
async def test_client_terminate_and_retry():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.method] = request.url.path
        return httpx.Response(200, text="")

    client = api.ConductorClient("http://x/api")
    client._client = httpx.AsyncClient(base_url="http://x/api",
                                       transport=httpx.MockTransport(handler))
    await client.terminate("w1", "stop")
    await client.retry("w1")
    assert seen["DELETE"] == "/api/workflow/w1"
    assert seen["POST"] == "/api/workflow/w1/retry"
    await client.aclose()


@pytest.mark.asyncio
async def test_client_signal_uses_oss_task_reference_sync_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tasks/w1/design_review__1/COMPLETED/sync"
        assert not request.url.params
        assert json.loads(request.content) == {"approved": True}
        return httpx.Response(200, json={"workflowId": "w1"})

    client = api.ConductorClient("http://x/api")
    client._client = httpx.AsyncClient(base_url="http://x/api",
                                       transport=httpx.MockTransport(handler))
    await client.signal_task("w1", "design_review__1", "COMPLETED", {"approved": True})
    await client.aclose()


@pytest.mark.asyncio
async def test_client_signal_falls_back_for_older_servers():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/sync"):
            return httpx.Response(404, json={"message": "endpoint not found"})
        assert json.loads(request.content) == {"review": "ship it"}
        return httpx.Response(200, text="task-id")

    client = api.ConductorClient("http://x/api")
    client._client = httpx.AsyncClient(base_url="http://x/api",
                                       transport=httpx.MockTransport(handler))
    await client.signal_task("w1", "review_gate", "COMPLETED", {"review": "ship it"})
    assert paths == [
        "/api/tasks/w1/review_gate/COMPLETED/sync",
        "/api/tasks/w1/review_gate/COMPLETED",
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_client_signal_rejects_successful_noop():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    client = api.ConductorClient("http://x/api")
    client._client = httpx.AsyncClient(base_url="http://x/api",
                                       transport=httpx.MockTransport(handler))
    with pytest.raises(api.ConductorError, match="no pending task was advanced"):
        await client.signal_task("w1", "review_gate", "COMPLETED", {"approved": True})
    await client.aclose()


@pytest.mark.asyncio
async def test_client_signal_explains_legacy_human_checkpoint():
    client = api.ConductorClient("http://x/api")
    with pytest.raises(api.ConductorError, match="legacy HUMAN checkpoint"):
        await client.signal_task(
            "w1", "review_gate", "COMPLETED", {"approved": True}, task_type="HUMAN"
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_client_uses_env_key_secret_for_every_api_request(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_AUTH_KEY", "test-key")
    monkeypatch.setenv("CONDUCTOR_AUTH_SECRET", "test-secret")
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/token":
            assert "X-Authorization" not in request.headers
            assert json.loads(request.content) == {
                "keyId": "test-key",
                "keySecret": "test-secret",
            }
            return httpx.Response(200, json={"token": "test-token"})
        assert request.headers["X-Authorization"] == "test-token"
        if request.method == "GET":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, text="wf-authenticated")

    client = api.ConductorClient("http://x/api")
    client._client = httpx.AsyncClient(base_url="http://x/api",
                                       transport=httpx.MockTransport(handler))
    assert await client.search_runs() == []
    assert await client.start("code_parallel", {"repoPath": "/tmp/repo"}) == "wf-authenticated"
    assert seen == [
        ("POST", "/api/token"),
        ("GET", "/api/workflow/search"),
        ("POST", "/api/workflow/code_parallel"),
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_client_refreshes_rejected_token_once(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_AUTH_KEY", "test-key")
    monkeypatch.setenv("CONDUCTOR_AUTH_SECRET", "test-secret")
    token_calls = 0
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, api_calls
        if request.url.path == "/api/token":
            token_calls += 1
            return httpx.Response(200, json={"token": f"token-{token_calls}"})
        api_calls += 1
        if api_calls == 1:
            assert request.headers["X-Authorization"] == "token-1"
            return httpx.Response(401)
        assert request.headers["X-Authorization"] == "token-2"
        return httpx.Response(200, json={"results": []})

    client = api.ConductorClient("http://x/api")
    client._client = httpx.AsyncClient(base_url="http://x/api",
                                       transport=httpx.MockTransport(handler))
    assert await client.search_runs() == []
    assert token_calls == 2
    assert api_calls == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_health_propagates_authorization_failure(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    client = api.ConductorClient("http://x/api")
    client._client = httpx.AsyncClient(
        base_url="http://x/api", transport=httpx.MockTransport(handler))

    with pytest.raises(api.ConductorError, match="HTTP 403"):
        await client.health()
    await client.aclose()


def test_client_rejects_partial_env_auth_without_exposing_value(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_AUTH_KEY", "do-not-render-this")

    with pytest.raises(AuthConfigurationError) as caught:
        api.ConductorClient("http://x/api")

    message = str(caught.value)
    assert "CONDUCTOR_AUTH_SECRET" in message
    assert "do-not-render-this" not in message
