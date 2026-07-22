"""Tools the chat agent can call, and the async dispatcher that runs them.

Read-only tools (list/get/gh) run freely; mutations (start/terminate/retry) go through
a confirm callback the screen supplies. All operate on the same ConductorClient the rest
of the TUI uses — the harness's guardrails are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from .. import catalog, format as fmt, gh
from ..api import ConductorClient, ConductorError

# Workflows the agent may start (the launchable set).
_STARTABLE = catalog.LAUNCHABLE


def _required_inputs(workflow: str) -> list[str]:
    spec = catalog.CATALOG.get(workflow)
    if not spec:
        return []
    keys: list[str] = []
    for f in spec.fields:
        if f.required:
            keys.extend(f.targets)
    return keys


TOOLS = [
    {
        "name": "list_runs",
        "description": "List recent harness runs (workflows) with status, target, tokens, cost.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["ALL", "RUNNING", "FAILED"],
                           "description": "filter; default ALL"},
                "limit": {"type": "integer", "description": "max rows (default 20)"},
            },
        },
    },
    {
        "name": "get_run",
        "description": "Get one run's status, per-task progress, tokens/cost, and result "
                       "(PR/review URL) by workflow id.",
        "input_schema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    {
        "name": "start_workflow",
        "description": (
            "Start exactly one harness workflow for the current user turn. Requires "
            "confirmation from the user (the host enforces it). If the user's request is "
            "ambiguous between workflows, do not call this tool; ask which single workflow "
            "they want. Workflows and their REQUIRED inputs: "
            "pr_review{repo, prNumber}, issue_to_pr{repo, issueNumber}, "
            "address_pr{repo, prNumber}, code_parallel{repoPath, instruction}. "
            "code_parallel, issue_to_pr, and address_pr with engine=code_parallel always plan "
            "through OpenSpec first — there is no design toggle to ask about. "
            "Optional inputs (agent/backend, engine, openspecHumanApproval, model, base, …) "
            "may be included; anything omitted uses the workflow default. pr_review and "
            "issue_to_pr pause for human review by default when started here (the drafted "
            "comments / PR are shown before they post); pass approve:false (pr_review) or "
            "approvePr:false (issue_to_pr) to run them unattended."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow": {"type": "string", "enum": _STARTABLE},
                "inputs": {"type": "object", "description": "workflow input key/values"},
            },
            "required": ["workflow", "inputs"],
        },
    },
    {
        "name": "register_workflows",
        "description": (
            "Register or update all harness task and workflow definitions on the selected "
            "Conductor server, then run the SIMPLE-task worker gate. Use this when the user "
            "asks to register, re-register, update, or refresh workflows. Requires confirmation."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "terminate_run",
        "description": "Terminate a running workflow (requires confirmation).",
        "input_schema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    {
        "name": "retry_run",
        "description": "Retry a failed workflow from its last failed task (requires confirmation).",
        "input_schema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    {
        "name": "list_issues",
        "description": "List open GitHub issues for a repo (owner/name or URL) via gh.",
        "input_schema": {"type": "object", "properties": {"repo": {"type": "string"}},
                         "required": ["repo"]},
    },
    {
        "name": "list_prs",
        "description": "List open GitHub pull requests for a repo (owner/name or URL) via gh.",
        "input_schema": {"type": "object", "properties": {"repo": {"type": "string"}},
                         "required": ["repo"]},
    },
]

MUTATIONS = {"start_workflow", "register_workflows", "terminate_run", "retry_run"}


@dataclass
class ToolContext:
    client: ConductorClient
    confirm: Callable[[str, str], Awaitable[bool]]   # (title, message) -> confirmed?
    on_run_started: Callable[[str], None]            # register a launched workflow id
    server_url: str = ""
    workflow_started: bool = False                   # host-enforced: max one per user turn


async def dispatch(name: str, tool_input: dict, ctx: ToolContext) -> str:
    """Run a tool; return a plain-text result for the model. Never raises."""
    try:
        return await _run(name, tool_input or {}, ctx)
    except ConductorError as e:
        return f"error: {e}"
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}: {e}"


async def _run(name: str, i: dict, ctx: ToolContext) -> str:
    if name == "list_runs":
        return await _list_runs(i, ctx)
    if name == "get_run":
        return await _get_run(i, ctx)
    if name == "start_workflow":
        return await _start(i, ctx)
    if name == "register_workflows":
        return await _register(ctx)
    if name == "terminate_run":
        return await _terminate(i, ctx)
    if name == "retry_run":
        return await _retry(i, ctx)
    if name in ("list_issues", "list_prs"):
        return await _gh(name, i)
    return f"error: unknown tool {name!r}"


async def _list_runs(i: dict, ctx: ToolContext) -> str:
    limit = int(i.get("limit") or 20)
    status = (i.get("status") or "ALL").upper()
    runs = await ctx.client.search_runs(limit=max(limit, 50))
    running = {"RUNNING", "SCHEDULED", "IN_PROGRESS", "PAUSED"}
    picked = []
    for r in runs:
        if status == "RUNNING" and r.status not in running:
            continue
        if status == "FAILED" and not (r.status.startswith("FAIL") or r.status == "TIMED_OUT"):
            continue
        picked.append(r)
        if len(picked) >= limit:
            break
    if not picked:
        return "no runs match."
    lines = [f"{r.id}  {r.status:10} {r.workflow:13} {r.target}" for r in picked]
    return "\n".join(lines)


async def _get_run(i: dict, ctx: ToolContext) -> str:
    d = await ctx.client.get_run(i["workflow_id"], recurse=True, only_running=False)
    tokens, cost = d.tokens_cost()
    lines = [f"{d.run.workflow} {d.run.id} — {d.run.status} · {d.run.target} · "
             f"{fmt.tokens(tokens)} tok · {fmt.cost(cost)}"]
    if d.run.reason:
        lines.append(f"reason: {d.run.reason}")
    for t in d.tasks:
        lines.append(f"  {t.ref:14} {t.status}")
    card = catalog.result_for(d.run.workflow, d.run.output)
    if card:
        for label, value in card.rows:
            lines.append(f"  · {label}: {value}")
        if card.primary_url:
            lines.append(f"  · url: {card.primary_url}")
    changes = d.file_changes()
    if changes:
        lines.append("files (A=created M=updated D=deleted):")
        for status, path in changes[:30]:
            lines.append(f"  {status} {path}")
        if len(changes) > 30:
            lines.append(f"  … +{len(changes) - 30} more")
    return "\n".join(lines)


async def _start(i: dict, ctx: ToolContext) -> str:
    if ctx.workflow_started:
        return ("error: one workflow has already been started for this user turn. "
                "Do not start another; ask the user which single workflow they want next.")
    wf = i.get("workflow")
    inputs = i.get("inputs") or {}
    if wf not in catalog.CATALOG:
        return f"error: unknown workflow {wf!r}. Choose one of: {', '.join(_STARTABLE)}"
    missing = [k for k in _required_inputs(wf) if k not in inputs or inputs.get(k) in (None, "")]
    if missing:
        return f"missing required inputs for {wf}: {', '.join(missing)} — ask the user for them."
    # Gate by default when launched interactively (chat), matching the form launcher.
    # The caller can still opt out by passing approve/approvePr explicitly.
    if wf == "pr_review" and "approve" not in inputs:
        inputs["approve"] = True
    if wf == "issue_to_pr" and "approvePr" not in inputs:
        inputs["approvePr"] = True
    target = catalog.target_for(wf, inputs)
    pretty = ", ".join(f"{k}={v}" for k, v in inputs.items())
    ok = await ctx.confirm(f"Start {wf}", f"{target}\ninputs: {pretty}")
    if not ok:
        return "user declined to start the workflow."
    # Set this before the network call. If the response is interrupted after Conductor accepts
    # the request, a second tool call must not create a duplicate run.
    ctx.workflow_started = True
    wid = await ctx.client.start(wf, inputs)
    ctx.on_run_started(wid)
    gated = (wf == "pr_review" and inputs.get("approve")) or \
            (wf == "issue_to_pr" and inputs.get("approvePr"))
    hint = (" It will pause for your review before anything is posted/opened — "
            "open it with `o` to approve, edit, or reject." if gated
            else " Tell the user they can open it with `o`.")
    return f"started {wf} — workflow id {wid} (target {target}).{hint}"


async def _register(ctx: ToolContext) -> str:
    if not ctx.server_url:
        return "error: selected Conductor server URL is unavailable."
    ok = await ctx.confirm(
        "Update workflow definitions",
        f"Register all harness task and workflow definitions on\n{ctx.server_url}?",
    )
    if not ok:
        return "user declined workflow registration."
    from ..registration import register_definitions
    result = await register_definitions(ctx.server_url)
    if result.ok:
        return f"workflow registration complete.\n{result.output}"
    return f"workflow registration failed.\n{result.output}"


async def _terminate(i: dict, ctx: ToolContext) -> str:
    wid = i["workflow_id"]
    ok = await ctx.confirm("Terminate run", f"Terminate {wid}? Agents stop; pushed work is kept.")
    if not ok:
        return "user declined to terminate."
    await ctx.client.terminate(wid, i.get("reason") or "terminated from chat")
    return f"terminated {wid}."


async def _retry(i: dict, ctx: ToolContext) -> str:
    wid = i["workflow_id"]
    ok = await ctx.confirm("Retry run", f"Retry {wid} from its last failed task?")
    if not ok:
        return "user declined to retry."
    await ctx.client.retry(wid)
    return f"retry requested for {wid}."


async def _gh(name: str, i: dict) -> str:
    repo = i.get("repo") or ""
    items = await (gh.list_prs(repo) if name == "list_prs" else gh.list_issues(repo))
    if items is None:
        return "gh unavailable or not authenticated."
    if not items:
        return "none open."
    kind = "PR" if name == "list_prs" else "issue"
    return "\n".join(f"#{n} {t}" for n, t in items) or f"no open {kind}s."
