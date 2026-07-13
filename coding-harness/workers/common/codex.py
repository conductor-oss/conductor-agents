"""OpenAI Codex backend for the coding_agent worker.

Primary driver: the official **`openai-codex` Python SDK** (`AsyncCodex` controlling
the local codex app-server over JSON-RPC — the wheel bundles its own runtime and
reuses the existing `~/.codex` auth). Native async (runs directly on the worker's
AsyncTaskRunner event loop), native `output_schema` structured output, `effort`
pass-through, `thread_resume` for sessions, and typed per-item notifications for the
live progress trace. Verified against openai-codex 0.1.0b3.

Fallback driver: the original `codex exec` CLI shelling (`_run_codex_cli`), selected
when the SDK isn't importable or `CODEX_DRIVER=cli` is set — an escape hatch while
the SDK is beta. Both map into the SAME uniform result dict `run_coding_agent`
returns for every backend.

SDK notification stream (per `AsyncTurnHandle.stream()`):
  turn/started, item/agentMessage/delta,
  item/completed  (payload.item.root.type: agentMessage | commandExecution |
                   fileChange | reasoning | webSearch | mcpToolCall | ...),
  thread/tokenUsage/updated (payload.token_usage: ThreadTokenUsage), turn/completed.

Gotchas baked in: Codex enforces OpenAI *strict* JSON Schema (objects need
additionalProperties:false and every property in `required`) — `_strictify_schema`
normalizes arbitrary schemas for BOTH drivers. Unattended runs use
`ApprovalMode.deny_all` (nothing ever blocks on an approval; the sandbox governs,
exactly like non-interactive `codex exec`). The CLI fallback additionally needs
`stdin=DEVNULL` (hangs otherwise).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import subprocess
import tempfile
import time
from typing import Any

from .cost import price_usage

log = logging.getLogger("coding_agent.codex")

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
# "sdk" (default) or "cli" — escape hatch while the openai-codex SDK is beta.
CODEX_DRIVER = os.environ.get("CODEX_DRIVER", "sdk").strip().lower()


def _strictify_schema(node: Any) -> Any:
    """Normalize a JSON Schema for OpenAI strict structured-output: every object gets
    additionalProperties:false and lists ALL its properties in `required`; a property
    that was NOT originally required becomes nullable so the model can still omit it."""
    if not isinstance(node, dict):
        return node
    out = copy.deepcopy(node)
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        props = out["properties"]
        original_required = set(out.get("required") or [])
        out["additionalProperties"] = False
        out["required"] = list(props.keys())
        for key, sub in props.items():
            props[key] = _strictify_schema(sub)
            if key not in original_required:
                t = props[key].get("type")
                if isinstance(t, str) and t != "null":
                    props[key]["type"] = [t, "null"]
                elif isinstance(t, list) and "null" not in t:
                    props[key]["type"] = t + ["null"]
    if "items" in out:
        out["items"] = _strictify_schema(out["items"])
    return out


def _tokens(usage: dict[str, Any]) -> int:
    return (int(usage.get("input_tokens") or 0)
            + int(usage.get("output_tokens") or 0)
            + int(usage.get("reasoning_output_tokens") or 0))


def _item_command(item: dict[str, Any]) -> str | None:
    """Human-readable one-liner for a Codex item (for the turns/commands trace).
    Accepts both the CLI event shapes (snake_case types) and the SDK thread-item
    shapes (camelCase types)."""
    it = item.get("type")
    if it in ("command_execution", "commandExecution") and item.get("command"):
        cmd = str(item["command"])
        return f"$ {cmd[:200]}"
    if it in ("file_change", "fileChange", "patch", "apply_patch"):
        path = item.get("path") or item.get("file") or ""
        if not path and isinstance(item.get("changes"), list):
            paths = [str(c.get("path", "")) for c in item["changes"] if isinstance(c, dict)]
            path = ", ".join(p for p in paths if p)[:200]
        return f"edit {path}".rstrip()
    if it in ("agent_message", "agentMessage"):
        return None  # captured as result text, not a command
    if it and it not in ("reasoning", "user_message", "userMessage"):
        return it  # surface any other item type by name
    return None


# --------------------------------------------------------------------------- SDK

async def _run_codex_sdk(
    prompt: str,
    *,
    worktree: str,
    model: str | None,
    write: bool,
    effort: str | None,
    output_schema: dict | None,
    resume_session_id: str | None,
    timeout_s: float | None,
    on_turn,
) -> dict[str, Any]:
    """Drive one turn via the openai-codex SDK, consuming the notification stream
    ourselves (mirroring the SDK's own _collect_async_turn_result) so we can fire
    on_turn progress per item AND collect the final result in a single pass."""
    from openai_codex import ApprovalMode, AsyncCodex, Sandbox

    err_base = {"result": "", "structured": None, "session_id": resume_session_id,
                "num_turns": 0, "turns": [], "tokens": 0, "cost_usd": 0.0,
                "denials": [], "turn_log": [], "stderr": ""}

    sandbox = Sandbox.workspace_write if write else Sandbox.read_only
    turn_kwargs: dict[str, Any] = {}
    if output_schema:
        turn_kwargs["output_schema"] = _strictify_schema(output_schema)
    if effort:
        turn_kwargs["effort"] = effort

    turns: list[dict[str, Any]] = []
    turn_log: list[str] = []
    session_id = resume_session_id
    cur_cmds: list[str] = []
    last_text = ""
    turn_idx = 0
    usage_total: dict[str, Any] = {}
    completed_status: str | None = None
    turn_error: str | None = None

    def _snapshot():
        return {"status": "IN_PROGRESS", "turns": _live_turns(), "numTurns": max(turn_idx, 1),
                "tokenUsed": _tokens(usage_total), "sessionId": session_id or ""}

    def _live_turns():
        live = list(turns)
        if cur_cmds:
            live.append({"turn": max(turn_idx, 1), "commands": list(cur_cmds), "tools": [],
                         "text": last_text[:300], "tokens": _tokens(usage_total)})
        return live

    def _fire():
        if on_turn:
            try:
                on_turn(_snapshot())
            except Exception:  # noqa: BLE001
                pass

    try:
        async with AsyncCodex() as codex:
            if resume_session_id:
                thread = await codex.thread_resume(
                    resume_session_id, cwd=worktree, sandbox=sandbox,
                    approval_mode=ApprovalMode.deny_all,
                    **({"model": model} if model else {}))
            else:
                thread = await codex.thread_start(
                    cwd=worktree, sandbox=sandbox,
                    approval_mode=ApprovalMode.deny_all,
                    **({"model": model} if model else {}))
            session_id = thread.id or session_id

            handle = await thread.turn(prompt, **turn_kwargs)

            async def _consume():
                nonlocal turn_idx, last_text, usage_total, completed_status, turn_error, cur_cmds
                async for event in handle.stream():
                    m = event.method
                    payload = event.payload
                    if m == "turn/started":
                        turn_idx += 1
                        continue
                    if m == "item/completed":
                        item_obj = getattr(payload, "item", None)
                        root = getattr(item_obj, "root", item_obj)
                        d = root.model_dump(by_alias=True) if hasattr(root, "model_dump") else {}
                        if d.get("type") == "agentMessage" and d.get("text"):
                            last_text = d["text"]
                        cmd = _item_command(d)
                        if cmd and cmd not in cur_cmds:
                            cur_cmds.append(cmd)
                            turn_log.append(f"turn={max(turn_idx, 1)} {cmd}")
                            _fire()
                        continue
                    if getattr(payload, "token_usage", None) is not None:
                        tu = payload.token_usage.total
                        usage_total = tu.model_dump() if hasattr(tu, "model_dump") else {}
                        _fire()
                        continue
                    if m == "turn/completed":
                        turn = payload.turn
                        completed_status = turn.status.value if turn.status else "unknown"
                        if turn.error is not None and getattr(turn.error, "message", None):
                            turn_error = turn.error.message
                        turns.append({"turn": max(turn_idx, 1), "commands": list(cur_cmds),
                                      "tools": [], "text": last_text[:300],
                                      "tokens": _tokens(usage_total)})
                        cur_cmds = []
                        _fire()

            # Timeout via a WATCHDOG that interrupts the turn server-side, NOT by
            # cancelling the stream: the SDK waits for notifications with a blocking
            # queue.get on a to_thread executor thread, and cancelling that await
            # abandons the thread mid-block (it then stalls loop shutdown for ~300s).
            # After interrupt() the server emits turn/completed(aborted), so the
            # stream ends naturally and no thread is ever left behind.
            timed_out = False

            async def _watchdog():
                nonlocal timed_out
                await asyncio.sleep(timeout_s)
                timed_out = True
                try:
                    await handle.interrupt()
                except Exception:  # noqa: BLE001
                    pass

            wd = asyncio.ensure_future(_watchdog()) if timeout_s else None
            try:
                if timeout_s:
                    # Last-resort cap for a server that ignores the interrupt —
                    # accepts the leaked-thread cost only in that pathological case.
                    await asyncio.wait_for(_consume(), timeout=timeout_s + 60)
                else:
                    await _consume()
            finally:
                if wd:
                    wd.cancel()
            if timed_out:
                return {"ok": False, "status": "timeout",
                        "error": f"codex exceeded {timeout_s}s",
                        **{**err_base, "turns": _live_turns(), "session_id": session_id,
                           "tokens": _tokens(usage_total), "turn_log": turn_log}}
    except FileNotFoundError as ex:
        return {"ok": False, "status": "codex_error",
                "error": f"codex runtime not found: {ex}", **err_base}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "status": "codex_error", "error": f"{type(ex).__name__}: {ex}",
                **{**err_base, "turns": _live_turns(), "session_id": session_id,
                   "tokens": _tokens(usage_total), "turn_log": turn_log}}

    ok = completed_status == "completed" and not turn_error
    structured = None
    if ok and output_schema and last_text.strip():
        try:
            structured = json.loads(last_text)
        except ValueError:
            structured = None

    tokens = _tokens(usage_total)
    cost = price_usage(
        {"input_tokens": usage_total.get("input_tokens", 0),
         "output_tokens": (usage_total.get("output_tokens", 0)
                           + usage_total.get("reasoning_output_tokens", 0)),
         "cache_read_input_tokens": usage_total.get("cached_input_tokens", 0)},
        model or "codex",
    ) if usage_total else 0.0

    return {
        "ok": ok,
        "status": "success" if ok else (completed_status or "codex_error"),
        "result": last_text.strip(),
        "structured": structured,
        "model": model or "",
        "session_id": session_id,
        "num_turns": max(turn_idx, 1 if turns else 0),
        "turns": turns,
        "tokens": tokens,
        "cost_usd": round(cost, 6),
        "denials": [],
        "turn_log": turn_log,
        "error": turn_error if not ok else None,
        "stderr": "",
    }


# --------------------------------------------------------------------------- CLI (fallback)

def _run_codex_cli(
    prompt: str,
    *,
    worktree: str,
    model: str | None = None,
    write: bool = True,
    output_schema: dict | None = None,
    resume_session_id: str | None = None,
    timeout_s: float | None = None,
    on_turn=None,
) -> dict[str, Any]:
    """Legacy driver: run one `codex exec` to completion, streaming its JSONL.

    ``write`` selects the sandbox: False → ``-s read-only``, True →
    ``-s workspace-write``. ``resume_session_id`` uses ``codex exec resume <id>``.
    """
    err_base = {"result": "", "structured": None, "session_id": resume_session_id,
                "num_turns": 0, "turns": [], "tokens": 0, "cost_usd": 0.0,
                "denials": [], "turn_log": [], "stderr": ""}

    sandbox = "workspace-write" if write else "read-only"
    tmp = tempfile.mkdtemp(prefix=".cc-codex-", dir=worktree)
    out_file = ""
    args = [CODEX_BIN, "exec"]
    if resume_session_id:
        args += ["resume", resume_session_id]
    args += [prompt, "-C", worktree, "--skip-git-repo-check", "--json", "-s", sandbox]
    if model:
        args += ["-m", model]
    if output_schema:
        schema_file = os.path.join(tmp, "schema.json")
        out_file = os.path.join(tmp, "out.json")
        with open(schema_file, "w", encoding="utf-8") as f:
            json.dump(_strictify_schema(output_schema), f)
        args += ["--output-schema", schema_file, "-o", out_file]

    turns: list[dict[str, Any]] = []
    turn_log: list[str] = []
    session_id = resume_session_id
    usage: dict[str, Any] = {}
    last_text = ""
    errors: list[str] = []
    turn_idx = 0
    cur_cmds: list[str] = []
    stderr_tail = ""
    structured = None

    def _snapshot():
        return {"status": "IN_PROGRESS", "turns": list(turns), "numTurns": turn_idx,
                "tokenUsed": _tokens(usage), "sessionId": session_id or ""}

    proc = None
    try:
        proc = subprocess.Popen(
            args, cwd=worktree, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = (time.monotonic() + timeout_s) if timeout_s else None
        for line in proc.stdout:  # streams as Codex emits JSONL
            if deadline and time.monotonic() > deadline:
                proc.kill()
                return {"ok": False, "status": "timeout", "error": f"codex exceeded {timeout_s}s",
                        **{**err_base, "turns": turns, "session_id": session_id}}
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("type")
            if t == "thread.started":
                session_id = e.get("thread_id") or session_id
            elif t == "turn.started":
                turn_idx += 1
                cur_cmds = []
            elif t in ("item.started", "item.completed"):
                cmd = _item_command(e.get("item") or {})
                item = e.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    last_text = item["text"]
                if cmd and cmd not in cur_cmds:
                    cur_cmds.append(cmd)
                    turn_log.append(f"turn={turn_idx} {cmd}")
                    if on_turn:
                        # reflect the in-progress turn's commands live
                        snap_turns = turns + [{"turn": turn_idx, "commands": cur_cmds,
                                               "tools": [], "text": "", "tokens": 0}]
                        try:
                            on_turn({"status": "IN_PROGRESS", "turns": snap_turns,
                                     "numTurns": turn_idx, "tokenUsed": _tokens(usage),
                                     "sessionId": session_id or ""})
                        except Exception:  # noqa: BLE001
                            pass
            elif t == "turn.completed":
                usage = e.get("usage") or usage
                turns.append({"turn": turn_idx, "commands": cur_cmds, "tools": [],
                              "text": last_text[:300], "tokens": _tokens(e.get("usage") or {})})
                if on_turn:
                    try:
                        on_turn(_snapshot())
                    except Exception:  # noqa: BLE001
                        pass
            elif t in ("error", "turn.failed"):
                msg = e.get("message") or (e.get("error") or {}).get("message") or "codex error"
                errors.append(str(msg)[:500])
        proc.wait(timeout=30)
        stderr_tail = (proc.stderr.read() or "")[-4000:] if proc.stderr else ""
        # Read the structured output file BEFORE the finally cleans up its temp dir.
        if out_file and os.path.exists(out_file):
            try:
                with open(out_file, encoding="utf-8") as f:
                    structured = json.load(f)
            except (OSError, ValueError):
                structured = None
    except FileNotFoundError:
        return {"ok": False, "status": "codex_error",
                "error": f"codex CLI not found (looked for '{CODEX_BIN}')", **err_base}
    except Exception as ex:  # noqa: BLE001
        if proc:
            proc.kill()
        return {"ok": False, "status": "codex_error", "error": f"{type(ex).__name__}: {ex}",
                **{**err_base, "turns": turns, "session_id": session_id}}
    finally:
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    # `structured` was read inside the try (before temp-dir cleanup); result text is
    # the last agent_message (or the structured JSON when a schema was used).
    rc = proc.returncode if proc else 1
    ok = rc == 0 and not errors
    tokens = _tokens(usage)
    cost = price_usage(
        {"input_tokens": usage.get("input_tokens", 0),
         "output_tokens": (usage.get("output_tokens", 0) + usage.get("reasoning_output_tokens", 0)),
         "cache_read_input_tokens": usage.get("cached_input_tokens", 0)},
        model,
    ) if usage else 0.0
    result_text = last_text.strip() or (json.dumps(structured) if structured else "")
    return {
        "ok": ok,
        "status": "success" if ok else "codex_error",
        "result": result_text,
        "structured": structured,
        "model": model or "",
        "session_id": session_id,
        "num_turns": turn_idx,
        "turns": turns,
        "tokens": tokens,
        "cost_usd": round(cost, 6),
        "denials": [],
        "turn_log": turn_log,
        "error": "; ".join(errors) if errors else (None if ok else f"codex exited {rc}"),
        "stderr": stderr_tail,
    }


# --------------------------------------------------------------------------- entry

def _sdk_available() -> bool:
    if CODEX_DRIVER == "cli":
        return False
    try:
        import openai_codex  # noqa: F401
        return True
    except ImportError:
        return False


async def run_codex_agent(
    prompt: str,
    *,
    worktree: str,
    model: str | None = None,
    write: bool = True,
    effort: str | None = None,
    output_schema: dict | None = None,
    resume_session_id: str | None = None,
    timeout_s: float | None = None,
    on_turn=None,
) -> dict[str, Any]:
    """Run one Codex session to completion; return the uniform coding_agent dict.

    Uses the openai-codex SDK by default (native async — runs on the caller's event
    loop); falls back to shelling the codex CLI (on a worker thread) when the SDK
    isn't installed or ``CODEX_DRIVER=cli`` is set. ``effort`` is honored only by
    the SDK driver.
    """
    if _sdk_available():
        return await _run_codex_sdk(
            prompt, worktree=worktree, model=model, write=write, effort=effort,
            output_schema=output_schema, resume_session_id=resume_session_id,
            timeout_s=timeout_s, on_turn=on_turn)
    log.info("codex driver: CLI fallback (%s)",
             "CODEX_DRIVER=cli" if CODEX_DRIVER == "cli" else "openai_codex not installed")
    return await asyncio.to_thread(
        _run_codex_cli,
        prompt, worktree=worktree, model=model, write=write,
        output_schema=output_schema, resume_session_id=resume_session_id,
        timeout_s=timeout_s, on_turn=on_turn)
