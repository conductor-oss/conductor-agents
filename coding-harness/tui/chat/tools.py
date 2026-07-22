"""Tools the chat agent can call, and the async dispatcher that runs them.

Read-only tools (list/get/gh) run freely; mutations (start/terminate/retry) go through
a confirm callback the screen supplies. All operate on the same ConductorClient the rest
of the TUI uses — the harness's guardrails are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from .. import catalog, format as fmt, gh, templates
from ..model_profiles import ProfileError, choose_profile, snapshot_inputs
from ..api import ConductorClient, ConductorError

# Workflows the agent may start (the launchable set).
_STARTABLE = catalog.LAUNCHABLE


def _apply_model_choice(workflow: str, inputs: dict) -> dict:
    """Apply NLP-extracted explicit choices, otherwise the unique scoped policy."""
    result = dict(inputs)
    explicit_profile = str(result.get("modelProfile") or "").strip()
    repo = str(result.get("repo") or result.get("repoPath") or "")
    profile = choose_profile(workflow, repo, explicit=explicit_profile)
    if profile:
        variant = explicit_profile if explicit_profile in profile.data.get("profiles", {}) else str(profile.data.get("defaultProfile") or "standard")
        for key, value in snapshot_inputs(profile, profile_variant=variant).items():
            if not result.get(key): result[key] = value
        # A profile selection must be executable even for legacy/scheduled
        # workflows that only accept agent+model, so expose its first code tier
        # as the generic model override as well as preserving the full snapshot.
        if not result.get("model"):
            variant = str(result.get("modelProfile") or profile.data.get("defaultProfile") or "standard")
            role = ((profile.data.get("profiles") or {}).get(variant, {}).get("roles") or {}).get("code", {})
            tiers = role.get("tiers") or [role]
            if tiers and isinstance(tiers[0], dict): result["model"] = tiers[0].get("model") or ""
    model = str(result.pop("model", "") or "").strip()
    if not model: return result
    lowered = model.lower()
    agent = "codex" if lowered.startswith(("gpt", "o", "codex")) else "claude" if lowered.startswith("claude") else "gemini" if lowered.startswith("gemini") else ""
    if workflow in {"code_parallel", "issue_to_pr"}:
        result.update({"planModel": model, "codeModel": model, "planAgent": agent, "codeAgent": agent})
    elif workflow == "feature_campaign":
        result.update({"designModel": model, "planModel": model, "codeModel": model, "reviewModel": model, "designAgent": agent, "planAgent": agent, "codeAgent": agent, "reviewAgent": agent})
    else:
        result.update({"model": model, "agent": agent})
    return result


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
            "local_review{repoPath}, pr_review{repo, prNumber}, issue_to_pr{repo, issueNumber}, "
            "address_pr{repo, prNumber}, code_parallel{repoPath, instruction}, "
            "feature_campaign{repoPath, instruction}, "
            "openspec_development{specSource, changeId, repoPath?}; useSpecSourceWorkspace:true selects a local "
            "checked-out spec repository as the implementation worktree and publishes a draft PR. feature_campaign is checkpoint-first "
            "and never pushes or opens a PR. "
            "openspec_development accepts local paths, Git remotes, or public HTTPS archives, "
            "routes automatically unless executionMode is set, and may pause when it selects a campaign. "
            "Every coding workflow accepts keepWorktree (default true). GitHub workflows also "
            "accept optional repoPath for a local source checkout; the run uses an isolated worktree. "
            "For code_parallel, issue_to_pr, and address_pr with engine=code_parallel, "
            "inputs MUST include design:true or design:false after asking the user. "
            "local_review compares the supplied checkout to baseRemote/baseBranch and is read-only; "
            "it never commits, pushes, or posts. Optional inputs (agent/backend, engine, design, maxSubtasks, model, base, …) "
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
    {
        "name": "list_approvals",
        "description": "List every pending signal-based approval checkpoint across workflows.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "decide_approval",
        "description": "Approve, revise, or stop a pending WAIT checkpoint. Requires confirmation.",
        "input_schema": {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "action": {"type": "string", "enum": ["approve", "revise", "stop"]},
            "feedback": {"type": "string"},
        }, "required": ["task_id", "action"]},
    },
    {
        "name": "list_schedules",
        "description": "List the three GitHub automation schedules.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "save_schedule",
        "description": "Create or update a GitHub automation schedule. Requires confirmation.",
        "input_schema": {"type": "object", "properties": {
            "workflow": {"type": "string", "enum": ["pr_review_sweep", "pr_address_sweep", "issue_resolution_sweep"]},
            "repo": {"type": "string"}, "name": {"type": "string"},
            "cron": {"type": "string"}, "zone_id": {"type": "string"},
            "approval_mode": {"type": "string", "enum": ["human", "llm"]},
            "model_profile": {"type": "string", "description": "optional user model-policy name"},
            "model": {"type": "string", "description": "optional concrete model ID"},
        }, "required": ["workflow", "repo"]},
    },
    {
        "name": "schedule_action",
        "description": "Pause, resume, delete, or run an automation schedule immediately. Requires confirmation.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"},
            "action": {"type": "string", "enum": ["pause", "resume", "delete", "run_now"]},
        }, "required": ["name", "action"]},
    },
    {
        "name": "reset_automation_item",
        "description": "Clear a stopped/exhausted automation item for an explicit revision. Requires confirmation.",
        "input_schema": {"type": "object", "properties": {
            "repo": {"type": "string"}, "kind": {"type": "string", "enum": ["review", "address", "issue"]},
            "number": {"type": "integer"}, "revision": {"type": "string"},
        }, "required": ["repo", "kind", "number", "revision"]},
    },
]

MUTATIONS = {"start_workflow", "register_workflows", "terminate_run", "retry_run",
             "decide_approval", "save_schedule", "schedule_action", "reset_automation_item"}


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
    if name == "list_approvals":
        return await _list_approvals(ctx)
    if name == "decide_approval":
        return await _decide_approval(i, ctx)
    if name == "list_schedules":
        return await _list_schedules(ctx)
    if name == "save_schedule":
        return await _save_schedule(i, ctx)
    if name == "schedule_action":
        return await _schedule_action(i, ctx)
    if name == "reset_automation_item":
        return await _reset_item(i, ctx)
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
    inputs = catalog.normalize_local_paths(i.get("inputs") or {})
    if wf not in catalog.CATALOG:
        return f"error: unknown workflow {wf!r}. Choose one of: {', '.join(_STARTABLE)}"
    try:
        inputs = _apply_model_choice(wf, inputs)
    except ProfileError as exc:
        return f"error: {exc} No workflow was started."
    uses_code_parallel = wf in ("code_parallel", "issue_to_pr") or (
        wf == "address_pr" and inputs.get("engine", "code_parallel") == "code_parallel"
    )
    if uses_code_parallel and not isinstance(inputs.get("design"), bool):
        return (f"design choice required for {wf}: ask the user whether to create design "
                "docs, then pass design:true or design:false. No workflow was started.")
    missing = [k for k in _required_inputs(wf) if k not in inputs or inputs.get(k) in (None, "")]
    if missing:
        return f"missing required inputs for {wf}: {', '.join(missing)} — ask the user for them."
    if not await ctx.client.workflow_registered(wf):
        return (
            f"error: {wf} is missing or stale on the selected Conductor server. "
            "Run /register, then start it again. No workflow was started."
        )
    # Gate by default when launched interactively (chat), matching the form launcher.
    # The caller can still opt out by passing approve/approvePr explicitly.
    if wf == "pr_review" and "approve" not in inputs:
        inputs["approve"] = True
    if wf == "issue_to_pr" and "approvePr" not in inputs:
        inputs["approvePr"] = True
    try:
        inputs, applied_templates = templates.apply_user_templates(wf, inputs)
    except templates.TemplateSelectionError as exc:
        return f"error: {exc} No workflow was started."
    target = catalog.target_for(wf, inputs)
    pretty_parts = []
    for key, value in inputs.items():
        if key.endswith("PromptTemplateSource"):
            continue
        if key.endswith("PromptTemplate"):
            pretty_parts.append(f"{key}=<{inputs.get(f'{key}Source', 'input:inline')}>")
        else:
            pretty_parts.append(f"{key}={value}")
    pretty = ", ".join(pretty_parts)
    detail = f"{target}\ninputs: {pretty}"
    if applied_templates:
        detail += "\ntemplates: " + ", ".join(
            f"{item.field} ← {item.source}" for item in applied_templates)
    from ..workspace import preview
    workspace_path = inputs.get("specSource", "") if (
        wf == "openspec_development" and inputs.get("useSpecSourceWorkspace")) else inputs.get("repoPath", "")
    workspace = preview(workspace_path) if wf != "local_review" else None
    if workspace:
        detail += (
            f"\nsource checkout: {workspace.source}"
            f"\nrun workspace: {workspace.planned}"
            f"\nsource changes ignored: {workspace.ignored_changes}"
            f"\nkeep worktree: {inputs.get('keepWorktree', True)}"
        )
    ok = await ctx.confirm(f"Start {wf}", detail)
    if not ok:
        return "user declined to start the workflow."
    # Set this before the network call. If the response is interrupted after Conductor accepts
    # the request, a second tool call must not create a duplicate run.
    ctx.workflow_started = True
    wid = await ctx.client.start(wf, inputs)
    ctx.on_run_started(wid)
    gated = (wf == "pr_review" and inputs.get("approve")) or \
            (wf == "issue_to_pr" and inputs.get("approvePr")) or wf == "feature_campaign"
    if wf == "openspec_development":
        hint = (" It may pause at feature-campaign checkpoints — "
                "open it with `o` to review or respond.")
    elif gated:
        hint = (" It will pause at its next review checkpoint — "
                "open it with `o` to approve, edit, or reject.")
    else:
        hint = " Tell the user they can open it with `o`."
    template_note = ""
    if applied_templates:
        template_note = " Templates: " + ", ".join(
            f"{item.field}={item.source}" for item in applied_templates) + "."
    return f"started {wf} — workflow id {wid} (target {target}).{template_note}{hint}"


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


async def _list_approvals(ctx: ToolContext) -> str:
    items = await ctx.client.pending_approvals()
    if not items:
        return "no approvals are waiting."
    return "\n".join(
        f"{item.task_id}  {item.workflow}  {item.task_ref}  owner={item.workflow_id}"
        + ("  LEGACY HUMAN — re-register" if item.legacy else "")
        for item in items
    )


async def _decide_approval(i: dict, ctx: ToolContext) -> str:
    items = await ctx.client.pending_approvals()
    item = next((x for x in items if x.task_id == i.get("task_id")), None)
    if not item:
        return "error: approval task not found (it may already be resolved)."
    if item.legacy:
        return "error: legacy HUMAN checkpoint; re-register current workflows and relaunch."
    action = i.get("action")
    feedback = str(i.get("feedback") or "").strip()
    if action == "revise" and not feedback:
        return "error: revise requires actionable feedback."
    confirmed = await ctx.confirm("Decide approval", f"{action} {item.workflow} checkpoint {item.task_ref}?")
    if not confirmed:
        return "user declined the approval decision."
    draft = item.draft
    if action == "approve":
        output = {"approved": True, "action": "approve"}
        if item.workflow == "pr_review": output["review"] = draft
        elif item.workflow == "issue_to_pr": output.update({"title": draft.get("title", ""), "body": draft.get("body", "")})
        else: output["artifact"] = draft
    else:
        output = {"approved": False, "action": action, "feedback": feedback,
                  "suppressed": action == "stop"}
    revision_capable = item.workflow in ("pr_review", "address_pr")
    status = "COMPLETED" if action == "approve" or revision_capable \
        else "FAILED_WITH_TERMINAL_ERROR"
    await ctx.client.signal_task(item.workflow_id, item.task_ref, status, output,
                                 task_type=item.task_type)
    return f"{action} recorded for {item.task_id}."


async def _list_schedules(ctx: ToolContext) -> str:
    from ..screens.automations import AUTOMATIONS
    items = [x for x in await ctx.client.list_schedules() if x.workflow in AUTOMATIONS]
    if not items:
        return "no GitHub automation schedules."
    return "\n".join(f"{x.name}  {'paused' if x.paused else 'active'}  {x.workflow}  {x.cron}  {x.zone_id}  {x.input.get('repo','')}" for x in items)


async def _save_schedule(i: dict, ctx: ToolContext) -> str:
    from ..screens.automations import build_schedule
    selected = _apply_model_choice(i["workflow"], {"repo": i["repo"], "modelProfile": i.get("model_profile") or "", "model": i.get("model") or ""})
    payload = build_schedule(i["workflow"], i["repo"], cron=i.get("cron") or "0 */10 * ? * *",
                             zone_id=i.get("zone_id"), approval_mode=i.get("approval_mode") or "human",
                             name=i.get("name") or "", workflow_input=selected)
    try:
        schedule_input, applied = templates.apply_user_templates(
            i["workflow"], payload["startWorkflowRequest"]["input"])
    except templates.TemplateSelectionError as exc:
        return f"error: {exc} No schedule was saved."
    payload["startWorkflowRequest"]["input"] = schedule_input
    detail = f"{payload['name']}\n{payload['cronExpression']} {payload['zoneId']}"
    if applied:
        detail += "\ntemplates: " + ", ".join(
            f"{item.field} ← {item.source}" for item in applied)
    confirmed = await ctx.confirm("Save automation schedule", detail)
    if not confirmed:
        return "user declined schedule save."
    await ctx.client.save_schedule(payload)
    return f"saved schedule {payload['name']}."


async def _schedule_action(i: dict, ctx: ToolContext) -> str:
    items = await ctx.client.list_schedules()
    item = next((x for x in items if x.name == i.get("name")), None)
    if not item:
        return "error: schedule not found."
    action = i["action"]
    confirmed = await ctx.confirm(f"{action} schedule", f"{action} {item.name}?")
    if not confirmed:
        return "user declined schedule mutation."
    if action == "delete": await ctx.client.delete_schedule(item.name)
    elif action == "pause": await ctx.client.pause_schedule(item.name, True)
    elif action == "resume": await ctx.client.pause_schedule(item.name, False)
    else:
        wid = await ctx.client.run_schedule_now(item); ctx.on_run_started(wid)
        return f"started {wid} from {item.name}."
    return f"{action} completed for {item.name}."


async def _reset_item(i: dict, ctx: ToolContext) -> str:
    confirmed = await ctx.confirm("Reset automation item", f"Reset {i['kind']} #{i['number']} revision {i['revision']}?")
    if not confirmed:
        return "user declined reset."
    wid = await ctx.client.start("automation_reset", i)
    ctx.on_run_started(wid)
    return f"reset requested in workflow {wid}."
