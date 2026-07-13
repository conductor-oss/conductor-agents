"""Autonomous coding agent — a faithful Python implementation of the reference
configuration in ``docs/CLAUDE_AGENT_SDK.md`` §17.

Where ``common/claude.py``'s ``run_agent`` uses ``acceptEdits`` and an open tool
surface, this module locks the agent down the way the doc recommends for an
UNATTENDED worker:

  * ``permission_mode="dontAsk"`` — anything not pre-approved is denied outright
    rather than hanging on a prompt that no human will answer (doc §5).
  * an explicit ``allowed_tools`` surface + bare-name ``disallowed_tools`` to keep
    the network and git-mutation tools out of the agent's context (doc §6).
  * a non-bypassable ``PreToolUse`` hook that denies any file write escaping the
    worktree — the ONLY check that runs on every tool call (doc §5, §7.1).
  * the ``claude_code`` system-prompt preset with ``exclude_dynamic_sections`` so a
    fleet of workers shares one prompt-cache entry (doc §12).
  * ``setting_sources`` pinned + auto-memory disabled for reproducibility (doc §10).
  * optional forced JSON via ``output_format`` (doc §11).
  * circuit breakers (``max_turns`` / ``max_budget_usd``) plus an EXTERNAL
    wall-clock timeout, because the SDK has none of its own (doc §13, §16).
  * retry == resume: on ``error_max_turns`` / ``error_max_budget_usd`` the caller
    resumes ``session_id`` from the SAME ``cwd`` with raised limits (doc §17).

``run_coding_agent`` returns a uniform dict:
    {ok, status, result, structured, session_id, num_turns, tokens, cost_usd,
     files_written, denials, turn_log, error}
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Any

import claude_agent_sdk as sdk

from .cost import usage_tokens

log = logging.getLogger("coding_agent")

# Directories never worth listing in the file-tree prime (noise + huge).
_TREE_SKIP_DIRS = {".git", ".cc-worktrees", "node_modules", ".venv", "venv",
                   "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
                   "dist", "build", "target", ".idea", ".vscode", ".next", ".gradle"}


def _file_tree(root: str, *, max_files: int = 400, max_chars: int = 8000) -> str:
    """Cheap, bounded listing of the working directory to prime the agent so it
    doesn't spend its first turn on `ls -R` / `Glob **/*`. Prefers `git ls-files`
    (respects .gitignore, includes untracked-but-not-ignored); falls back to a
    filtered walk for non-git dirs. Bounded in count + chars so a big repo can't
    bloat the prompt — a trailing note tells the agent the list was truncated."""
    files: list[str] = []
    try:
        r = subprocess.run(
            ["git", "-C", root, "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0:
            files = [f for f in r.stdout.splitlines() if f.strip()]
    except Exception:  # noqa: BLE001 — priming is best-effort, never fatal
        files = []
    if not files:  # non-git dir, or git unavailable — bounded walk
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _TREE_SKIP_DIRS]
                for fn in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, fn), root)
                    files.append(rel)
                    if len(files) > max_files * 2:  # cap the walk itself
                        break
                if len(files) > max_files * 2:
                    break
        except OSError:
            return ""
    files = sorted(set(files))
    total = len(files)
    if total == 0:
        return "(the working directory is currently empty)"
    shown = files[:max_files]
    text = "\n".join(shown)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0]
        shown = text.splitlines()
    remaining = total - len(shown)
    if remaining > 0:
        text += f"\n… and {remaining} more file(s) (list truncated — use Glob/Grep for the rest)"
    return text

# Availability trim (doc §6): only these built-ins exist in the agent's context at
# all. Everything else (AskUserQuestion, TaskCreate/Update, Monitor, Agent, Skill,
# WebSearch/WebFetch, NotebookEdit, ...) is removed — cheaper per request and no
# wasted turns attempting tools that would only be denied.
DEFAULT_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]

# Read-only tool surface — used to derive the Codex sandbox (read-only vs write).
_READ_ONLY_TOOLS = {"Read", "Grep", "Glob"}


def _infer_backend(backend: str | None, model: str | None) -> str:
    """Explicit `backend` wins; else infer from the model id; default 'claude'.
    gpt-*/o[1-4]*/codex-* → codex; gemini-* → gemini; claude-* → claude."""
    if backend:
        return backend.strip().lower()
    m = (model or "").lower()
    if m.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return "codex"
    if m.startswith("gemini"):
        return "gemini"
    return "claude"


def _is_write_surface(tools: list[str] | None) -> bool:
    """True unless the tool surface is restricted to read-only tools. Lets a
    read-only planner (tools=[Read,Grep,Glob]) map to Codex's read-only sandbox."""
    if not tools:
        return True
    return not set(tools).issubset(_READ_ONLY_TOOLS)

# The coding surface an unattended worker gets. Read/search/edit + a scoped set of
# safe Bash verbs. Note the scoped Bash rules approve ONLY matching commands; any
# other Bash call falls through to dontAsk and is denied (doc §5).
DEFAULT_ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(python *)", "Bash(python3 *)", "Bash(node *)", "Bash(npm *)",
    "Bash(npx *)", "Bash(cat *)", "Bash(ls *)", "Bash(pytest *)",
    "Bash(go *)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)",
    # File move/delete/reorg: Claude Code has no delete/move tool, and Write/Edit can
    # only create/modify — so "move X" / "delete file" tasks need these shell verbs.
    # mkdir/cp/touch are here too because the agent chains them (e.g.
    # `mkdir -p sub && mv a sub/`), and Claude Code requires EVERY sub-command of a
    # compound command to be allowed. Bounded by the OS sandbox (writes confined to the
    # worktree); the deny list still blocks `rm -rf`/`sudo` (deny wins over allow).
    "Bash(git mv *)", "Bash(git rm *)", "Bash(mv *)", "Bash(rm *)",
    "Bash(mkdir *)", "Bash(cp *)", "Bash(touch *)",
]

# Bare names remove the tool from Claude's context entirely (doc §6). Scoped git
# deny rules block mutation even if a Bash allow rule would otherwise match.
DEFAULT_DISALLOWED_TOOLS = [
    "WebSearch", "WebFetch",
    "Bash(git push*)", "Bash(git commit*)", "Bash(git reset*)",
    "Bash(rm -rf *)", "Bash(sudo *)",
]

WORKER_SYSTEM_APPEND = (
    "You are an unattended conductor-code worker. There is no human to answer "
    "prompts. Work only inside the current working directory. You may create, edit, "
    "move, and delete files within it (use `git mv`/`git rm`/`mv`/`rm` for moves and "
    "deletions). Do not commit or push — the harness owns git — and do not run "
    "system-destructive commands (`rm -rf`, `sudo`). Finish the task in as few tool "
    "calls as possible."
)


def _worktree_guard(worktree_abs: str) -> sdk.HookMatcher:
    """A PreToolUse hook that denies Write/Edit escaping the worktree.

    This is the inner-layer guard the doc calls the only check that runs on EVERY
    tool call. It denies out-of-scope writes and passes everything else through
    ({} == no decision) so the permission flow (dontAsk + allowed_tools) decides.
    """
    root = os.path.realpath(worktree_abs)

    async def guard(input_data, tool_use_id, context):  # noqa: ANN001
        try:
            tool_input = input_data.get("tool_input") or {}
            path = tool_input.get("file_path")
            if path:
                target = os.path.realpath(path if os.path.isabs(path)
                                          else os.path.join(root, path))
                if target != root and not target.startswith(root + os.sep):
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                f"Write to {target} escapes the worktree {root}; "
                                "stay inside your working directory."
                            ),
                        }
                    }
        except Exception as e:  # noqa: BLE001 — a hook must never crash the agent
            log.warning("worktree guard error (allowing): %s", e)
        return {}

    # Matcher is an exact-name alternation (doc §7.1): fires only for file writers.
    return sdk.HookMatcher(matcher="Write|Edit|NotebookEdit", hooks=[guard])


def _summarize_tool(block: sdk.ToolUseBlock) -> str:
    """Human-readable one-liner for a single tool call, surfacing the salient arg.

    Bash → the command; file tools → the path; search tools → the pattern; anything
    else → a compact JSON of its input. This is what shows up in each turn's
    ``commands`` list."""
    import json as _json

    name = block.name
    inp = block.input if isinstance(block.input, dict) else {}
    if name == "Bash":
        return f"$ {inp.get('command', '')}".rstrip()
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Read"):
        return f"{name} {inp.get('file_path') or inp.get('notebook_path') or ''}".rstrip()
    if name in ("Glob", "Grep"):
        return f"{name} {inp.get('pattern', '')}".rstrip()
    if name == "Agent":
        return f"Agent {inp.get('subagent_type', '')}: {inp.get('description', '')}".rstrip()
    # Fallback: tool name + compact input (capped so a huge arg can't bloat output).
    blob = _json.dumps(inp, ensure_ascii=False)
    if len(blob) > 300:
        blob = blob[:300] + "…"
    return f"{name} {blob}".rstrip()


async def _drive(prompt: str, options: sdk.ClaudeAgentOptions,
                 on_turn=None) -> dict[str, Any]:
    """Iterate the query to completion, collecting turns, denials and the result.

    Drains the stream fully rather than breaking on ResultMessage — trailing system
    events can arrive after it (doc §3, gotcha #20).
    """
    result: sdk.ResultMessage | None = None
    session_id: str | None = None
    turns: list[dict[str, Any]] = []      # structured per-turn record (the output array)
    turn_log: list[str] = []              # one-line strings for the Conductor logs tab
    denials: list[str] = []
    turn = 0

    stream_error: str | None = None
    try:
        async for msg in sdk.query(prompt=prompt, options=options):
            if isinstance(msg, sdk.SystemMessage):
                # session_id is nested in SystemMessage.data in Python (doc §10).
                if msg.subtype == "init":
                    session_id = (msg.data or {}).get("session_id")
            elif isinstance(msg, sdk.AssistantMessage):
                turn += 1
                blocks = msg.content or []
                tool_blocks = [b for b in blocks if isinstance(b, sdk.ToolUseBlock)]
                commands = [_summarize_tool(b) for b in tool_blocks]
                # A short slice of the assistant's prose that accompanied the tool calls,
                # so a turn with no commands (thinking / final summary) still says something.
                text = " ".join(
                    b.text.strip() for b in blocks if isinstance(b, sdk.TextBlock) and b.text.strip()
                )
                tok = usage_tokens(msg.usage) if msg.usage else 0
                turns.append({
                    "turn": turn,
                    "commands": commands,
                    "tools": [b.name for b in tool_blocks],
                    "text": (text[:300] + "…") if len(text) > 300 else text,
                    "tokens": tok,
                })
                turn_log.append(f"turn={turn} commands={commands or '[]'} tokens={tok}")
                if on_turn is not None:
                    # Interim snapshot for progress reporting. Cost isn't known until
                    # the ResultMessage, so report the running token sum meanwhile.
                    try:
                        on_turn({
                            "status": "IN_PROGRESS",
                            "turns": list(turns),
                            "numTurns": turn,
                            "tokenUsed": sum(t["tokens"] for t in turns),
                            "sessionId": session_id or "",
                        })
                    except Exception:  # noqa: BLE001 — never let reporting break the loop
                        pass
            elif isinstance(msg, sdk.ResultMessage):
                result = msg
    except Exception as e:  # noqa: BLE001
        # SDK doc gotcha #19: on an error result (error_max_turns / error_max_budget_usd)
        # the CLI exits nonzero and the stream raises AFTER yielding the ResultMessage —
        # as a bare Exception("Claude Code returned an error result: ..."), not a
        # ClaudeSDKError (see _internal/query.py receive_messages). Keep everything we
        # collected — losing session_id here would break retry-as-resume exactly when
        # it's needed. Re-raise only if we truly got nothing (a genuine
        # startup/connection failure).
        if result is None and session_id is None:
            raise
        stream_error = f"{type(e).__name__}: {e}"

    if result is None:
        return {"ok": False, "status": "no_result", "result": "", "structured": None,
                "session_id": session_id, "num_turns": turn, "turns": turns, "tokens": 0,
                "cost_usd": 0.0, "denials": denials, "turn_log": turn_log,
                "error": stream_error or "no ResultMessage from agent"}

    denials = [
        f"{d.get('tool_name', '?')}: {d.get('tool_input', '')}"
        for d in (getattr(result, "permission_denials", None) or [])
    ]
    return {
        "ok": not result.is_error,
        "status": result.subtype,
        "result": result.result or "",
        "structured": result.structured_output,
        "session_id": result.session_id or session_id,
        "num_turns": result.num_turns,
        "turns": turns,
        "tokens": usage_tokens(result.usage),
        "cost_usd": result.total_cost_usd or 0.0,
        "denials": denials,
        "turn_log": turn_log,
        "error": ("; ".join(result.errors) if getattr(result, "errors", None) else None)
                 or stream_error,
    }


async def run_coding_agent(
    prompt: str,
    *,
    worktree: str,
    model: str | None = None,
    fallback_model: str | None = None,
    effort: str | None = None,
    max_turns: int = 50,
    max_budget_usd: float | None = 5.0,
    timeout_s: float | None = None,
    resume_session_id: str | None = None,
    output_schema: dict | None = None,
    tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    setting_sources: list[str] | None = None,
    sandbox_enabled: bool = True,
    allowed_domains: list[str] | None = None,
    session_store: Any = None,
    on_turn=None,
    include_file_tree: bool = True,
    include_repo_guide: bool = True,
    backend: str | None = None,
) -> dict[str, Any]:
    """Run one locked-down autonomous coding session to completion (async).

    Runs directly on the caller's event loop — under conductor-python's
    AsyncTaskRunner every concurrent coding_agent task shares ONE loop, so the
    blocking pieces (Codex subprocess, git file-tree listing) are pushed to
    threads via ``asyncio.to_thread`` and everything else awaits natively.

    ``worktree`` is both the working directory AND the write boundary enforced by
    the guard hook. ``timeout_s`` is an external wall-clock cap (the SDK has none);
    on expiry the query is abandoned and ``session_id`` is returned so the caller
    can resume. ``resume_session_id`` MUST be paired with the same ``worktree`` it
    was created in, or the resume silently starts fresh (doc §10 #1 bug).

    ``sandbox_enabled`` (default True) wraps every Bash command in the OS sandbox
    (Seatbelt on macOS, bubblewrap on Linux): writes confined to the workspace and
    NO network unless ``allowed_domains`` opens specific hosts (e.g.
    ["registry.npmjs.org"] for a task that must npm-install). This closes the
    interpreter-escape hole the guard hook can't see — ``python -c`` writing outside
    the tree or opening sockets fails at the OS level, not by convention.
    ``fallback_model`` picks up when the primary model is overloaded.

    ``setting_sources`` controls which filesystem config loads (doc §10). Default
    ``["project"]`` loads the repo's CLAUDE.md conventions — but ALSO its
    ``.claude/settings.json`` hooks/allow-rules, a repo-controlled injection vector.
    For untrusted repos pass ``[]`` (operators can flip the default via the
    ``CODING_AGENT_SETTING_SOURCES`` env in the worker task).

    ``session_store`` (a SessionStore adapter, e.g. ``FileSessionStore`` on a shared
    volume) mirrors transcripts so a DIFFERENT worker host can resume this session;
    without it, sessions are host-local and cross-host ``resume_session_id`` starts
    fresh (doc §10). Returns ``stderr`` (tail of the subprocess's stderr) for
    diagnosability when a run fails opaquely.

    ``on_turn(snapshot)`` is invoked after each turn with a partial-output dict
    ({status, turns, numTurns, tokenUsed, sessionId}) so a caller can stream progress
    (the worker uses it to push interim IN_PROGRESS updates to Conductor).

    ``include_file_tree`` (default True) prepends a bounded listing of the working
    directory to the prompt so the agent skips the reflexive first-turn `ls`/`Glob`
    — saving a model round-trip and its tokens. Skipped on resume (the session
    already has the tree).
    """
    import collections
    # realpath canonicalizes symlinks (on macOS /tmp -> /private/tmp) so the guard
    # hook compares like-for-like paths. Create the working dir if missing — a
    # greenfield "write an app here" task shouldn't require the caller to mkdir first;
    # the write-boundary guard still confines the agent to this tree.
    worktree_abs = os.path.realpath(worktree)
    try:
        os.makedirs(worktree_abs, exist_ok=True)
    except OSError as e:
        return {"ok": False, "status": "bad_worktree", "result": "", "structured": None,
                "session_id": None, "num_turns": 0, "tokens": 0, "cost_usd": 0.0,
                "denials": [], "turn_log": [], "error": f"cannot create worktree {worktree_abs}: {e}"}

    # Prime the prompt with the current file listing so the agent doesn't burn its
    # first turn discovering the directory structure. Only on a cold start — a
    # resumed session already knows the tree.
    if include_file_tree and not resume_session_id:
        # `git ls-files` can take seconds on a big repo — off the shared loop.
        tree = await asyncio.to_thread(_file_tree, worktree_abs)
        if tree:
            prompt = (f"Files currently in the working directory (relative paths):\n"
                      f"{tree}\n\n{prompt}")

    # Backend dispatch: everything above is engine-agnostic (worktree prep + file-tree
    # prime). Codex and Gemini are driven via their CLIs (common/codex.py,
    # common/gemini.py) and return the same result dict; Claude-only options below
    # (system prompt, hooks, session store, effort, allowed_domains, tool allow/deny
    # lists) don't apply to them — see the parity matrix in docs/CODING_AGENT_WORKER.md.
    # Both are blocking subprocess drivers (can run for minutes) — a worker thread,
    # not the shared event loop; each enforces timeout_s itself.
    be = _infer_backend(backend, model)
    # Cross-backend model guard: an explicit `agent` often arrives with another
    # backend's model id (e.g. code_parallel's default codeModel is a claude-* id
    # while codeAgent="gemini") — the engine would 404 on it. Fall back to the
    # chosen backend's own default model instead of failing the task.
    if model and _infer_backend(None, model) != be:
        log.warning("model %r belongs to a different backend than %r — using %s's default model",
                    model, be, be)
        model = None

    # Repo "agent guide" prime (AGENTS.md → AGENT.md → CLAUDE.md): how to build/test/review this
    # repo, injected into the prompt for ALL backends and the read-only review step. Cold start
    # only (a resumed session already saw it). should_inject_guide skips CLAUDE.md for Claude when
    # it already loads it via setting_sources (avoids a double-load); AGENTS.md always injects.
    if include_repo_guide and not resume_session_id:
        from .templating import read_repo_guide, should_inject_guide
        guide = await asyncio.to_thread(read_repo_guide, worktree_abs)
        if guide and should_inject_guide(be, setting_sources, guide[0]):
            name, text = guide
            prompt = (f"Repository guide ({name}) — authoritative conventions for building, "
                      f"testing, and reviewing this repository; follow it:\n\n{text}\n\n---\n\n{prompt}")

    if be == "codex":
        from .codex import run_codex_agent
        # Native async (openai-codex SDK; internally falls back to the CLI on a
        # worker thread). Passes `effort` through — honored by the SDK driver.
        return await run_codex_agent(
            prompt, worktree=worktree_abs, model=model,
            write=_is_write_surface(tools), effort=effort,
            output_schema=output_schema, resume_session_id=resume_session_id,
            timeout_s=timeout_s, on_turn=on_turn,
        )
    if be == "gemini":
        from .gemini import run_gemini_agent
        # Blocking subprocess driver — a worker thread, not the shared event loop.
        res = await asyncio.to_thread(
            run_gemini_agent,
            prompt, worktree=worktree_abs, model=model,
            write=_is_write_surface(tools),
            output_schema=output_schema, resume_session_id=resume_session_id,
            timeout_s=timeout_s, on_turn=on_turn,
        )
        res.setdefault("model", model or "")
        return res

    system_prompt: dict[str, Any] = {
        "type": "preset",
        "preset": "claude_code",
        "append": WORKER_SYSTEM_APPEND,
        "exclude_dynamic_sections": True,
    }

    # Bounded stderr capture — a ring buffer so a chatty subprocess can't grow
    # memory without bound; only the tail is ever useful for diagnosis.
    stderr_lines: collections.deque[str] = collections.deque(maxlen=200)

    opts: dict[str, Any] = {
        "cwd": worktree_abs,
        "add_dirs": [worktree_abs],
        "system_prompt": system_prompt,
        "setting_sources": setting_sources if setting_sources is not None else ["project"],
        # env MERGES on top of the inherited env in Python (doc gotcha #17), so this
        # keeps ANTHROPIC_API_KEY/PATH while disabling auto-memory for reproducibility.
        "env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
        "permission_mode": "dontAsk",
        # Availability trim: only these built-ins exist in context (doc §6).
        "tools": tools if tools is not None else DEFAULT_TOOLS,
        "allowed_tools": allowed_tools if allowed_tools is not None else DEFAULT_ALLOWED_TOOLS,
        "disallowed_tools": disallowed_tools if disallowed_tools is not None else DEFAULT_DISALLOWED_TOOLS,
        "hooks": {"PreToolUse": [_worktree_guard(worktree_abs)]},
        "max_turns": max_turns,
        "stderr": stderr_lines.append,
    }
    if session_store is not None:
        opts["session_store"] = session_store
    if sandbox_enabled:
        # OS-level containment for Bash (doc §14). Both bypass valves are shut:
        # the model cannot run a command unsandboxed (dangerouslyDisableSandbox is
        # refused) and sandboxing does NOT auto-approve Bash — the explicit
        # allowlist above still gates which commands run at all.
        opts["sandbox"] = {
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
            "network": {"allowedDomains": allowed_domains or []},
        }
    if model:
        opts["model"] = model
    if fallback_model:
        opts["fallback_model"] = fallback_model
    if effort:
        opts["effort"] = effort
    if max_budget_usd is not None:
        opts["max_budget_usd"] = max_budget_usd
    if resume_session_id:
        opts["resume"] = resume_session_id
    if output_schema:
        opts["output_format"] = {"type": "json_schema", "schema": output_schema}

    options = sdk.ClaudeAgentOptions(**opts)

    def _attach(d: dict[str, Any]) -> dict[str, Any]:
        # Tail of subprocess stderr, capped, for diagnosing opaque failures.
        d["stderr"] = "".join(stderr_lines)[-4000:]
        d.setdefault("model", model or "")
        return d

    err_base = {"result": "", "structured": None, "num_turns": 0, "turns": [],
                "tokens": 0, "cost_usd": 0.0, "denials": [], "turn_log": []}
    try:
        coro = _drive(prompt, options, on_turn=on_turn)
        if timeout_s:
            return _attach(await asyncio.wait_for(coro, timeout=timeout_s))
        return _attach(await coro)
    except asyncio.TimeoutError:
        return _attach({"ok": False, "status": "timeout", "session_id": resume_session_id,
                        "error": f"agent exceeded external timeout of {timeout_s}s", **err_base})
    except sdk.ClaudeSDKError as e:
        return _attach({"ok": False, "status": "sdk_error", "session_id": resume_session_id,
                        "error": f"{type(e).__name__}: {e}", **err_base})
