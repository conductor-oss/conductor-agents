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

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"


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
