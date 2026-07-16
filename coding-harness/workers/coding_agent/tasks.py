"""coding_agent — an UNATTENDED Claude Agent SDK coding worker.

This is the doc §17 reference configuration wired to Conductor: given a worktree
and a task prompt, it runs one locked-down autonomous session
(``dontAsk`` + explicit allowlist + a worktree-escape guard hook) and reports
what changed, cost, and the session id for retry-as-resume.

Inputs (task.input_data):
  prompt            (required) the task instruction for the agent
  worktreePath      (required) working dir AND the write boundary
  agent             (optional) backend: "claude" (default) or "codex". If unset,
                    inferred from model id (gpt-*/o*/codex-* -> codex).
  model             (optional) model id; omit for the CLI/SDK default
  fallbackModel     (optional) model to fall back to when the primary is overloaded
  effort            (optional) low|medium|high|xhigh|max
  maxTurns          (optional) tool-use round-trip cap (default 50)
  maxBudgetUsd      (optional) spend cap (default 50.0)
  resumeSessionId   (optional) resume a prior session — MUST use the same worktree
  schema            (optional) JSON Schema (dict or JSON string) forcing structured output
  allowedDomains    (optional) network domains the OS sandbox may reach (list or
                    comma-separated), e.g. "registry.npmjs.org" for npm install.
                    Default: none — sandboxed commands have NO network access.
  promptTemplate    (optional) full-override prompt text, OR "@repo/rel/path" to read the
                    prompt from a file in the worktree. When set it REPLACES the built-in
                    `prompt`; {{key}} placeholders are filled from promptContext.
  includeRepoGuide  (optional, default true) prepend the repo guide (AGENTS.md/AGENT.md/
                    CLAUDE.md, if present in the worktree) to the prompt. Env override:
                    CODING_AGENT_REPO_GUIDE=0 disables it fleet-wide.
  templateKey       (optional) when promptTemplate is empty, look for a repo-resident
                    override at <worktree>/.conductor/<templateKey>.md (see resolve_prompt).
  promptContext     (optional) map of named runtime values ({diff, feedback, instruction,
                    files, …}) used to fill {{key}} placeholders in the chosen template;
                    unused non-empty entries are appended under a "## Context" trailer.
                    Precedence: promptTemplate > repo .conductor/<key>.md > built-in prompt.

Output: filesChanged, result/structured, sessionId, turns, tokenUsed, costUsd,
        denials, status. Never raises for an agent that merely failed — the
        status field carries the SDK result subtype so the workflow can branch.
"""

from __future__ import annotations

import asyncio
import json as _json
import os

from conductor.client.context.task_context import get_task_context
from conductor.client.worker.worker_task import worker_task

from common import git
from common.coding_agent import _infer_backend, run_coding_agent
from common.progress import ProgressReporter
from common.results import cap, fail, ok
from common.session_store import store_from_env
from common.templating import resolve_prompt
from common.tool_policy import denied_without_changes

# Operator knob (doc §10 item 5): which filesystem settings load. Default "project"
# loads the repo's CLAUDE.md conventions but also its .claude/settings.json
# hooks/allow-rules. For untrusted repos set CODING_AGENT_SETTING_SOURCES= (empty)
# or "none" so nothing repo-controlled reaches the agent. Per-task `settingSources`
# input overrides this.
_ENV_SETTING_SOURCES = os.environ.get("CODING_AGENT_SETTING_SOURCES")


def _env_num(name, default, cast):
    """Read a numeric operator knob from the env, falling back to ``default`` on
    unset/blank/non-numeric values (cast is int or float). Fleet-wide overrides for
    the per-task runtime caps below; the per-task input always wins over these."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


# Fleet-wide runtime defaults (doc §10). Each is the fallback when the per-task
# input omits the value; a per-task maxTurns/maxBudgetUsd still overrides these.
CODING_AGENT_MAX_TURNS = _env_num("CODING_AGENT_MAX_TURNS", 50, int)
CODING_AGENT_MAX_BUDGET_USD = _env_num("CODING_AGENT_MAX_BUDGET_USD", 50.0, float)
CODING_AGENT_HEARTBEAT_S = _env_num("CODING_AGENT_HEARTBEAT_S", 30.0, float)

# Shared-volume session store for cross-host resume (doc §10 item 6). Constructed
# once from CODING_AGENT_SESSION_STORE_DIR; None (and host-local sessions) if unset.
_SESSION_STORE = store_from_env()


def _parse_schema(schema):
    if isinstance(schema, str) and schema.strip():
        return _json.loads(schema)
    return schema or None


def _as_list(val):
    """Accept a list or comma-separated string; return a list or None if empty.
    Used for the tool-restriction inputs (tools/allowedTools/disallowedTools)."""
    if val is None:
        return None
    if isinstance(val, list):
        return val or None
    items = [x.strip() for x in str(val).split(",") if x.strip()]
    return items or None


def _resolve_setting_sources(task_val):
    """Precedence: per-task input > env > default ["project"]. An explicit empty
    value ("", "none", "[]", []) means load nothing (untrusted-repo lockdown)."""
    raw = task_val if task_val is not None else _ENV_SETTING_SOURCES
    if raw is None:
        return ["project"]
    if isinstance(raw, list):
        return raw
    s = str(raw).strip().lower()
    if s in ("", "none", "[]"):
        return []
    return [x.strip() for x in str(raw).split(",") if x.strip()]


@worker_task(task_definition_name="coding_agent", thread_count=8)
async def coding_agent():
    # async def → conductor-python routes this to its AsyncTaskRunner: one shared
    # event loop, thread_count=8 becomes a semaphore of 8 concurrent tasks. Keep
    # blocking work (git subprocesses, thread joins) off the loop via to_thread.
    #
    # No parameters: AsyncTaskRunner maps declared params to input_data KEYS (it
    # never passes the Task object — a `task` param would arrive as None). The
    # runner sets a per-execution TaskContext contextvar before awaiting us; the
    # Task (ids, input, to_task_result) comes from there.
    task = get_task_context().task
    i = task.input_data or {}
    try:
        wt = i.get("worktreePath") or ""
        if not wt:
            return fail(task, "coding_agent", "worktreePath is required")
        # Effective prompt: explicit promptTemplate > repo .conductor/<key>.md > built-in prompt.
        prompt = resolve_prompt(
            (i.get("prompt") or "").strip(),
            template=i.get("promptTemplate"),
            template_key=i.get("templateKey"),
            context=i.get("promptContext"),
            worktree=wt,
        )
        if not prompt.strip():
            return fail(task, "coding_agent",
                        "prompt is required (none of prompt, promptTemplate, or a repo template resolved)")

        domains = i.get("allowedDomains")
        if isinstance(domains, str):
            domains = [d.strip() for d in domains.split(",") if d.strip()]

        before = await asyncio.to_thread(git.status_files, wt)
        # Push interim IN_PROGRESS updates: after every turn (on_turn) and at least
        # every 30s regardless; all HTTP pushes happen on the reporter's own thread.
        reporter = ProgressReporter(task, heartbeat_s=CODING_AGENT_HEARTBEAT_S).start()
        try:
            res = await run_coding_agent(
                prompt,
                worktree=wt,
                model=i.get("model") or None,
                fallback_model=i.get("fallbackModel") or None,
                effort=i.get("effort") or None,
                max_turns=int(i.get("maxTurns") or CODING_AGENT_MAX_TURNS),
                max_budget_usd=(float(i["maxBudgetUsd"]) if i.get("maxBudgetUsd") is not None else CODING_AGENT_MAX_BUDGET_USD),
                resume_session_id=i.get("resumeSessionId") or None,
                output_schema=_parse_schema(i.get("schema")),
                allowed_domains=domains or None,
                setting_sources=_resolve_setting_sources(i.get("settingSources")),
                session_store=_SESSION_STORE,
                on_turn=reporter.update,
                # Optional tightening: restrict the tool surface (e.g. a read-only
                # planner passes tools=["Read","Grep","Glob"]). `tools` is an
                # availability gate — it can only remove built-ins, never add.
                tools=_as_list(i.get("tools")),
                allowed_tools=_as_list(i.get("allowedTools")),
                disallowed_tools=_as_list(i.get("disallowedTools")),
                # Prime the prompt with the dir listing (skips the agent's first
                # ls/Glob turn). Default on; set includeFileTree:false to disable.
                include_file_tree=(str(i.get("includeFileTree", "true")).lower()
                                   not in ("false", "0", "no")),
                # Prime the prompt with the repo guide (AGENTS.md/AGENT.md/CLAUDE.md) so the
                # agent knows how to build/test/review. Default on; includeRepoGuide:false or
                # the CODING_AGENT_REPO_GUIDE=0 worker env disables it.
                include_repo_guide=(str(i.get("includeRepoGuide", "true")).lower()
                                    not in ("false", "0", "no")),
                # Backend: "claude" (default) or "codex". If unset, inferred from the
                # model id (gpt-*/o*/codex-* → codex). See docs/CODING_AGENT_WORKER.md.
                backend=i.get("agent") or None,
            )
        finally:
            # stop() joins the heartbeat thread (up to ~2s) — off the loop.
            await asyncio.to_thread(reporter.stop)
        after = await asyncio.to_thread(git.status_files, wt)
        changed = sorted(after - before) or sorted(after)
        # Per-file status (A/M/D/R) for the changed set — additive, for review UIs.
        after_codes = await asyncio.to_thread(git.status_changes, wt)
        file_changes = [{"path": p, "status": after_codes.get(p, "M")} for p in changed]

        backend = _infer_backend(i.get("agent"), i.get("model"))
        logs = [
            f"[coding_agent] backend={backend} model={res.get('model') or '(default)'} status={res.get('status', '')} "
            f"{'resumed ' + str(i.get('resumeSessionId'))[:8] if i.get('resumeSessionId') else 'cold-start'} "
            f"session={str(res.get('session_id'))[:8]}",
            f"[coding_agent] changed={len(changed)} {changed} turns={res.get('num_turns', 0)} "
            f"tokens={res.get('tokens', 0)} cost=${res.get('cost_usd', 0.0):.4f}",
        ]
        for entry in res.get("turn_log") or []:
            logs.append(f"[coding_agent] {entry}")
        for d in res.get("denials") or []:
            logs.append(f"[coding_agent] DENIED {cap(d, 200)}")

        out = {
            "status": res.get("status", ""),
            "agent": backend,
            "model": res.get("model") or "",
            "filesChanged": changed,
            "fileChanges": file_changes,
            "result": cap(res.get("result"), 2000),
            "structured": res.get("structured"),
            "sessionId": res.get("session_id") or "",
            # `turns` is the per-turn array (turn number + commands run + tokens);
            # `numTurns` is the scalar count for quick reference.
            "turns": res.get("turns") or [],
            "numTurns": res.get("num_turns", 0),
            "tokenUsed": res.get("tokens", 0),
            "costUsd": res.get("cost_usd", 0.0),
            "denials": res.get("denials") or [],
        }

        # A model can end its turn normally after reporting that a required command was denied.
        # The SDK marks that as ok even though no work was produced. Fail closed so parent
        # workflows cannot commit/push a partial PR fix and announce that all feedback was
        # addressed (the PR #6 regression).
        if denied_without_changes(changed, out["denials"]):
            err = "agent made no changes after one or more tool denials"
            logs.append(f"[coding_agent] error: {err}")
            out["retryable"] = False
            return fail(task, "coding_agent", err, logs, output=out)

        if not res.get("ok"):
            err = res.get("error") or f"agent ended with status={res.get('status', '')}"
            # The agent's final text often carries the real reason the SDK's generic
            # stream error hides — e.g. an invalid/inaccessible model or an auth error.
            detail = (res.get("result") or "").strip()
            if detail:
                err = f"{err} — {detail[:300]}"
            logs.append(f"[coding_agent] error: {err}")
            if res.get("stderr"):
                logs.append(f"[coding_agent] stderr tail: {cap(res['stderr'], 2000)}")
            # error_max_turns / error_max_budget_usd are retryable via resume — surface
            # the session id and let the workflow decide, rather than hard-failing.
            out["retryable"] = res.get("status") in ("error_max_turns", "error_max_budget_usd")
            out["stderr"] = cap(res.get("stderr"), 2000)
            return fail(task, "coding_agent", err, logs, output=out)

        return ok(task, out, logs)
    except Exception as e:  # noqa: BLE001
        return fail(task, "coding_agent", e)
