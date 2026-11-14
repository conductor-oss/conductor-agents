"""Claude Agent SDK wrapper.

The harness drives coding/review/diagnosis through the Claude Agent SDK
(``claude-agent-sdk``), which runs the same ``claude`` engine the TS harness shelled
out to — but via a clean async Python API. This module serves SYNC workers only
(e.g. gitops' merge_worktrees), which run on conductor-python's thread-pool
TaskRunner where no event loop is running — so ``asyncio.run()`` here is safe.
Do NOT call ``run_agent`` from an async worker (that path runs on the shared
AsyncTaskRunner loop, where asyncio.run raises); async workers use
``common.coding_agent.run_coding_agent`` instead.

``run_agent`` returns a uniform dict:
    {ok, text, structured, cost_usd, tokens, files_changed, num_turns, error}
"""

from __future__ import annotations

import asyncio
from typing import Any

import claude_agent_sdk as sdk

from .cost import usage_tokens

# Read-only tool set for review/diagnosis (no edits).
READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "Bash"]
# Web tools add latency/cost and aren't needed for coding.
NO_WEB = ["WebSearch", "WebFetch"]


async def _drive(prompt: str, options: sdk.ClaudeAgentOptions,
                verbose: bool = False) -> dict[str, Any]:
    result: sdk.ResultMessage | None = None
    turn_log: list[str] = []
    turn = 0
    async for msg in sdk.query(prompt=prompt, options=options):
        if isinstance(msg, sdk.AssistantMessage):
            turn += 1
            tools = [b.name for b in (msg.content or []) if isinstance(b, sdk.ToolUseBlock)]
            tok = usage_tokens(msg.usage) if msg.usage else 0
            entry = f"turn={turn} tools={tools} tokens={tok}"
            turn_log.append(entry)
            if verbose:
                import logging; logging.getLogger("claude_agent").debug(entry)
        if isinstance(msg, sdk.ResultMessage):
            result = msg
    if result is None:
        return {"ok": False, "error": "no ResultMessage from agent", "text": "",
                "structured": None, "cost_usd": 0.0, "tokens": 0, "num_turns": 0,
                "session_id": None, "turn_log": turn_log}
    return {
        "ok": not result.is_error,
        "text": result.result or "",
        "structured": result.structured_output,
        "cost_usd": result.total_cost_usd or 0.0,
        "tokens": usage_tokens(result.usage),
        "num_turns": result.num_turns,
        "session_id": result.session_id,
        "error": "; ".join(result.errors) if result.errors else None,
        "turn_log": turn_log,
    }


# Exceptions worth retrying: transient network faults, connection resets, stalls.
def _is_retryable(e: Exception) -> bool:
    import anthropic
    import httpx
    if isinstance(e, (anthropic.APIConnectionError, anthropic.APITimeoutError,
                      httpx.TransportError, httpx.HTTPError, ConnectionError, TimeoutError)):
        return True
    if isinstance(e, OSError):  # includes [Errno 54] Connection reset by peer
        return True
    if isinstance(e, anthropic.APIStatusError):
        return e.status_code in (408, 409, 429, 500, 502, 503, 504)
    return False


def _retry(fn, attempts: int = 5, base: float = 2.0):
    """Call fn(); on a retryable network error, back off and retry."""
    import time
    last = None
    for a in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if not _is_retryable(e) or a == attempts - 1:
                raise
            last = e
            time.sleep(base * (a + 1))
    raise last  # unreachable


def run_agent(prompt: str, *, cwd: str | None = None, model: str | None = None,
              write: bool = False, add_dirs: list[str] | None = None,
              max_turns: int | None = None, resume_session_id: str | None = None,
              schema: dict | None = None, allowed_tools: list[str] | None = None,
              max_budget_usd: float | None = 50.0) -> dict[str, Any]:
    """Run a Claude Agent SDK session to completion (sync wrapper).

    write=True grants ``acceptEdits`` + the working dir as an editable root;
    write=False restricts to read-only tools (review / diagnosis).
    resume_session_id continues a prior session (keeps context — big token saver).
    schema: JSON Schema dict — passed as --json-schema for structured output.
    Runtime deadlines are owned exclusively by the Conductor task definition."""
    import json as _json
    opts: dict[str, Any] = {
        "model": model or None,
        "cwd": cwd,
        "disallowed_tools": NO_WEB,
    }
    if write:
        opts["permission_mode"] = "acceptEdits"
        opts["add_dirs"] = add_dirs or ([cwd] if cwd else [])
        # Optionally restrict the tool set even in write mode. Handing a doc-writer
        # only file tools (no Bash) means it can't wander into shell commands or hang
        # on one — faster and eliminates the subprocess-hang failure mode.
        if allowed_tools:
            opts["allowed_tools"] = allowed_tools
    else:
        opts["permission_mode"] = "default"
        opts["allowed_tools"] = allowed_tools or READ_ONLY_TOOLS
    if max_turns is not None:
        opts["max_turns"] = max_turns
    if max_budget_usd is not None:
        opts["max_budget_usd"] = max_budget_usd
    if resume_session_id:
        opts["resume"] = resume_session_id
    if schema:
        # extra_args must be a dict {flag: value} without leading '--'; SDK prepends it
        opts["extra_args"] = {"json-schema": _json.dumps(schema)}

    options = sdk.ClaudeAgentOptions(**{k: v for k, v in opts.items() if v is not None})

    err_base = {"text": "", "structured": None, "cost_usd": 0.0, "tokens": 0, "num_turns": 0, "session_id": None, "turn_log": []}
    try:
        return asyncio.run(_drive(prompt, options))
    except sdk.ClaudeSDKError as e:  # CLI not found, connection, JSON decode, process error
        return {"ok": False, "error": f"{type(e).__name__}: {e}", **err_base}
