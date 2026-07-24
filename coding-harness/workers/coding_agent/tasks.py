"""coding_agent — an UNATTENDED Claude Agent SDK coding worker.

This is the doc §17 reference configuration wired to Conductor: given a worktree
and a task prompt, it runs one locked-down autonomous session
(``dontAsk`` + explicit allowlist + a worktree-escape guard hook) and reports
what changed, cost, and the session id for retry-as-resume.

Inputs (task.input_data):
  prompt            (required) the task instruction for the agent
  worktreePath      (required) working dir AND the write boundary
  modelRole         (optional) policy role: design|plan|code|review|judge (default code)
  modelResolution   (optional) output from model_profile_resolve; direct internal runs
                    resolve the supplied policy envelope themselves when it is absent
  agent / model     (optional) explicit backend/model override for this one task role
  fallbackModel     (optional) model to fall back to when the primary is overloaded
  effort            (optional) low|medium|high|xhigh|max
  maxTurns          (optional) tool-use round-trip cap (default 50)
  maxBudgetUsd      (optional) spend cap (default 50.0)
  resumeSessionId   (optional) resume a prior session — MUST use the same worktree
  allowedWriteRoots (optional) repository-relative paths that tighten the normal
                    worktree write boundary (campaign tasks use their plan's files)
  contextFiles     (optional) internal OpenSpec snapshot files to append read-only;
                   every path must be below OPENSPEC_SNAPSHOT_DIR and the combined
                   content is capped at 512 KiB
  failSoft          (optional) report agent exhaustion/errors in output while completing
                    the worker task, so an interactive campaign can pause and resume it
  schema            (optional) JSON Schema (dict or JSON string) forcing structured output
  allowedDomains    (optional) network domains the OS sandbox may reach (list or
                    comma-separated), e.g. "registry.npmjs.org" for npm install.
                    Default: none — sandboxed commands have NO network access.
  promptTemplate    (optional) full-override prompt text, OR "@repo/rel/path" to read the
                    prompt from a file in the worktree. When set it REPLACES the built-in
                    `prompt`; {{key}} placeholders are filled from promptContext.
  promptTemplateSource (optional) caller-supplied provenance label. It is reported as
                    requestedSource but never used to resolve or trust prompt content.
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
        denials, status, and promptTemplate provenance (requested/resolved source,
        template key, SHA-256). Never raises for an agent that merely failed — the
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
from common.model_policy import ModelPolicyError, select_role_tier
from common.progress import ProgressReporter
from common.results import cap, fail, ok
from common.session_store import store_from_env
from common.templating import resolve_prompt_details
from common.tool_policy import denied_without_changes

# Operator knob (doc §10 item 5): which filesystem settings load. Default "project"
# loads the repo's CLAUDE.md conventions but also its .claude/settings.json
# hooks/allow-rules. For untrusted repos set CODING_AGENT_SETTING_SOURCES= (empty)
# or "none" so nothing repo-controlled reaches the agent. Per-task `settingSources`
# input overrides this.
_ENV_SETTING_SOURCES = os.environ.get("CODING_AGENT_SETTING_SOURCES")

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


def _bool(val, default=False):
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _within_write_roots(path: str, roots: list[str] | None) -> bool:
    if not roots:
        return True
    norm = os.path.normpath(path).replace("\\", "/").lstrip("./")
    for root in roots:
        base = os.path.normpath(str(root)).replace("\\", "/").lstrip("./").rstrip("/")
        if norm == base or norm.startswith(base + "/"):
            return True
    return False


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


def _append_context_files(prompt: str, value) -> str:
    files = [str(item).strip() for item in (_as_list(value) or []) if str(item).strip()]
    if not files:
        return prompt
    root = os.path.realpath(os.environ.get("OPENSPEC_SNAPSHOT_DIR", "/tmp/conductor-openspec"))
    chunks: list[str] = []
    total = 0
    for raw in files:
        path = os.path.realpath(str(raw))
        if path != root and not path.startswith(root + os.sep):
            raise ValueError(f"context file is outside OPENSPEC_SNAPSHOT_DIR: {raw}")
        if not os.path.isfile(path):
            raise ValueError(f"context file does not exist: {raw}")
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        total += len(content.encode())
        if total > 512 * 1024:
            raise ValueError("context files exceed 512 KiB")
        chunks.append(f"## Read-only external context: {os.path.basename(path)}\n{content}")
    return prompt.rstrip() + "\n\n# Authoritative external context\n\n" + "\n\n".join(chunks)


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
        role = str(i.get("modelRole") or "code").strip().lower()
        try:
            model_tier, model_resolution = select_role_tier(i, role=role, worktree=wt)
        except ModelPolicyError as exc:
            return fail(task, "coding_agent", exc)
        # Effective prompt: explicit promptTemplate > repo .conductor/<key>.md > built-in prompt.
        prompt_resolution = resolve_prompt_details(
            (i.get("prompt") or "").strip(),
            template=i.get("promptTemplate"),
            template_key=i.get("templateKey"),
            context=i.get("promptContext"),
            worktree=wt,
        )
        prompt = prompt_resolution.prompt
        prompt = _append_context_files(prompt, i.get("contextFiles"))
        if not prompt.strip():
            return fail(task, "coding_agent",
                        "prompt is required (none of prompt, promptTemplate, or a repo template resolved)")

        domains = i.get("allowedDomains")
        if isinstance(domains, str):
            domains = [d.strip() for d in domains.split(",") if d.strip()]

        write_roots = _as_list(i.get("allowedWriteRoots"))
        before = await asyncio.to_thread(git.status_files, wt)
        # Campaign runs can stay active for days; a 10-second heartbeat keeps task
        # ownership visible through worker restarts and long model turns.
        reporter = ProgressReporter(task, heartbeat_s=10.0).start()
        try:
            res = await run_coding_agent(
                prompt,
                worktree=wt,
                model=model_tier.get("model") or None,
                fallback_model=i.get("fallbackModel") or None,
                effort=i.get("effort") or None,
                max_turns=int(model_tier.get("maxTurns") or 50),
                max_budget_usd=(float(model_tier["maxBudgetUsd"]) if model_tier.get("maxBudgetUsd") is not None else 50.0),
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
                backend=model_tier["agent"],
                write_roots=write_roots,
            )
        finally:
            # stop() joins the heartbeat thread (up to ~2s) — off the loop.
            await asyncio.to_thread(reporter.stop)
        after = await asyncio.to_thread(git.status_files, wt)
        # Cross-backend fail-closed enforcement. Claude's hook rejects direct file-tool
        # escapes up front; Codex/Gemini and Bash are additionally reconciled here so a
        # campaign task can never hand unauthorized new changes to the commit step.
        unauthorized = sorted(p for p in (after - before)
                              if not _within_write_roots(p, write_roots))
        for path in unauthorized:
            await asyncio.to_thread(git.restore_path, wt, path)
        if unauthorized:
            res.setdefault("denials", []).append(
                "write roots: reverted out-of-scope changes: " + ", ".join(unauthorized))
            after = await asyncio.to_thread(git.status_files, wt)
        changed = sorted(after - before) or sorted(after)
        # Per-file status (A/M/D/R) for the changed set — additive, for review UIs.
        after_codes = await asyncio.to_thread(git.status_changes, wt)
        file_changes = [{"path": p, "status": after_codes.get(p, "M")} for p in changed]

        backend = _infer_backend(model_tier["agent"], model_tier.get("model"))
        logs = [
            f"[coding_agent] backend={backend} model={res.get('model') or '(default)'} status={res['status']} "
            f"{'resumed ' + str(i.get('resumeSessionId'))[:8] if i.get('resumeSessionId') else 'cold-start'} "
            f"session={str(res.get('session_id'))[:8]}",
            f"[coding_agent] prompt-template requested={i.get('promptTemplateSource') or '(auto)'} "
            f"resolved={prompt_resolution.source} key={prompt_resolution.template_key or '(none)'} "
            f"sha256={prompt_resolution.sha256[:12]}",
            f"[coding_agent] changed={len(changed)} {changed} turns={res['num_turns']} "
            f"tokens={res['tokens']} cost=${res['cost_usd']:.4f}",
        ]
        for entry in res.get("turn_log") or []:
            logs.append(f"[coding_agent] {entry}")
        for d in res.get("denials") or []:
            logs.append(f"[coding_agent] DENIED {cap(d, 200)}")

        out = {
            "status": res["status"],
            "agent": backend,
            "model": res.get("model") or "",
            "modelResolution": {
                "profile": model_resolution.get("profile", ""),
                "role": role,
                "tier": model_tier,
                "canonicalSha256": model_resolution.get("canonicalSha256", ""),
                "sources": model_resolution.get("sources", []),
                "warnings": model_resolution.get("warnings", []),
            },
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
            "promptTemplate": {
                "requestedSource": str(i.get("promptTemplateSource") or "auto"),
                "resolvedSource": prompt_resolution.source,
                "templateKey": prompt_resolution.template_key,
                "sha256": prompt_resolution.sha256,
            },
        }

        # A model can end its turn normally after reporting that a required command was denied.
        # The SDK marks that as ok even though no work was produced. Fail closed so parent
        # workflows cannot commit/push a partial PR fix and announce that all feedback was
        # addressed (the PR #6 regression).
        if denied_without_changes(changed, out["denials"]):
            err = "agent made no changes after one or more tool denials"
            logs.append(f"[coding_agent] error: {err}")
            out["retryable"] = False
            if _bool(i.get("failSoft"), False):
                out["error"] = err
                return ok(task, out, logs)
            return fail(task, "coding_agent", err, logs, output=out)

        if not res["ok"]:
            err = res.get("error") or f"agent ended with status={res['status']}"
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
            out["retryable"] = res["status"] in ("error_max_turns", "error_max_budget_usd")
            out["stderr"] = cap(res.get("stderr"), 2000)
            if _bool(i.get("failSoft"), False):
                out["error"] = err
                return ok(task, out, logs)
            return fail(task, "coding_agent", err, logs, output=out)

        return ok(task, out, logs)
    except Exception as e:  # noqa: BLE001
        return fail(task, "coding_agent", e)
