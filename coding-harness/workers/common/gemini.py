"""Google Gemini backend for the coding_agent worker.

Drives the official open-source **Gemini CLI** headless (`gemini -p … -o json`) as a
subprocess — the same architecture as the Codex backend (`codex exec`), and the same
thing the Claude Agent SDK does under the hood with the `claude` CLI. Google's Gen AI
SDK is a raw model API (no tools / agent loop / sandbox), so the CLI *is* Google's
coding-agent engine. Maps the CLI's output into the SAME uniform result dict
`run_coding_agent` returns for the other backends.

Verified against gemini-cli v0.49.0:
  * `-o json` → ONE JSON document on stdout:
      {"session_id": "<uuid>", "response": "<final text>",
       "stats": {"models": {"<model>": {"api": {...}, "tokens": {...}}},
                 "tools": {"totalCalls": N, "totalSuccess": N, "totalFail": N,
                            "byName": {"<tool>": {...}}},
                 "files": {"totalLinesAdded": N, "totalLinesRemoved": N}},
       "error": {"type": "...", "message": "...", "code": N}}   # only on error
  * `--approval-mode plan`  → read-only mode (maps the read-only planner surface)
    `--approval-mode yolo`  → auto-approve all tools (unattended write mode)
  * `-s/--sandbox` → OS sandbox for write mode: Seatbelt on macOS (default profile
    `permissive-open`: writes confined to the project folder, network open),
    docker/podman on Linux. NOTE the network stays open in the default profile —
    weaker than the Claude backend's no-network sandbox; see the parity matrix.
  * `--resume <id>` resumes a session; the JSON output carries `session_id`.
  * Auth: GEMINI_API_KEY in the worker env (or ~/.gemini/.env). Never logged.

Parity gaps (documented in docs/CODING_AGENT_WORKER.md §12): json mode is final-only —
no per-turn stream — so `turns` is a single synthesized summary from `stats.tools` and
`num_turns` approximates the tool-call count; structured output is prompt-enforced
(+ one corrective resume-retry), not schema-enforced like Codex.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any

from .cost import price_usage

log = logging.getLogger("coding_agent.gemini")

GEMINI_BIN = os.environ.get("GEMINI_BIN", "gemini")

_STRUCTURED_INSTRUCTION = (
    "\n\nRespond ONLY with a single JSON object that is valid against this JSON "
    "Schema — no prose, no markdown fences, nothing before or after the JSON:\n{schema}"
)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _extract_json(text: str) -> Any:
    """Parse a JSON object/array out of model prose: strip markdown fences, then fall
    back to the first '{'…last '}' span. Returns None when nothing parses."""
    if not text:
        return None
    candidate = _FENCE_RE.sub("", text).strip()
    for attempt in (candidate,):
        try:
            return json.loads(attempt)
        except ValueError:
            pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(candidate[start:end + 1])
        except ValueError:
            return None
    return None


def _sum_tokens(stats: dict[str, Any]) -> tuple[int, dict[str, int]]:
    """Total tokens across all models in `stats.models`, plus an input/output split
    for pricing. Field names are defensive: prefer `total`, else sum the parts."""
    total = 0
    inp = out = cached = 0
    for m in (stats.get("models") or {}).values():
        tok = (m or {}).get("tokens") or {}
        prompt = int(tok.get("prompt") or 0)
        candidates = int(tok.get("candidates") or 0)
        thoughts = int(tok.get("thoughts") or 0)
        tool = int(tok.get("tool") or 0)
        cache = int(tok.get("cached") or 0)
        t = int(tok.get("total") or 0) or (prompt + candidates + thoughts + tool)
        total += t
        inp += prompt + tool
        out += candidates + thoughts
        cached += cache
    return total, {"input_tokens": inp, "output_tokens": out,
                   "cache_read_input_tokens": cached}


def _tool_summary(stats: dict[str, Any]) -> tuple[int, list[str]]:
    """(totalCalls, per-tool one-liners) from `stats.tools` for the turns trace."""
    tools = stats.get("tools") or {}
    calls = int(tools.get("totalCalls") or 0)
    lines: list[str] = []
    for name, info in (tools.get("byName") or {}).items():
        n = info.get("count") if isinstance(info, dict) else None
        if n is None and isinstance(info, dict):
            n = info.get("totalCalls") or info.get("calls")
        n = int(n or 0) or ""
        lines.append(f"{name} x{n}" if n else str(name))
    return calls, lines


def _run_once(args: list[str], worktree: str, timeout_s: float | None):
    """Run the CLI once; return (rc, stdout, stderr_tail) or raise TimeoutExpired."""
    proc = subprocess.Popen(
        args, cwd=worktree, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return proc.returncode, stdout or "", (stderr or "")[-4000:]


def run_gemini_agent(
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
    """Run one headless Gemini CLI session to completion; return the uniform dict.

    ``write`` selects the surface: False → ``--approval-mode plan`` (read-only, the
    planner), True → ``--approval-mode yolo`` + ``--sandbox`` (auto-approved, edits
    confined to the worktree by the OS sandbox). ``on_turn`` fires once with the
    final snapshot (json mode has no per-turn stream; the worker's heartbeat covers
    liveness in between).
    """
    err_base = {"result": "", "structured": None, "session_id": resume_session_id,
                "num_turns": 0, "turns": [], "tokens": 0, "cost_usd": 0.0,
                "denials": [], "turn_log": [], "stderr": ""}

    full_prompt = prompt
    if output_schema:
        full_prompt += _STRUCTURED_INSTRUCTION.format(schema=json.dumps(output_schema))

    # --skip-trust: headless runs in untrusted dirs exit 55 otherwise; the harness
    # only ever points the agent at worktrees it created itself.
    args = [GEMINI_BIN, "-p", full_prompt, "-o", "json", "--skip-trust"]
    if model:
        args += ["-m", model]
    if write:
        args += ["--approval-mode", "yolo", "--sandbox"]
    else:
        args += ["--approval-mode", "plan"]
    if resume_session_id:
        args += ["--resume", resume_session_id]

    try:
        rc, stdout, stderr_tail = _run_once(args, worktree, timeout_s)
    except FileNotFoundError:
        return {"ok": False, "status": "gemini_error",
                "error": f"gemini CLI not found (looked for '{GEMINI_BIN}')", **err_base}
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout",
                "error": f"gemini exceeded {timeout_s}s", **err_base}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "status": "gemini_error",
                "error": f"{type(ex).__name__}: {ex}", **err_base}

    doc = _extract_json(stdout) if stdout.strip() else None
    if not isinstance(doc, dict):
        # Fatal errors (e.g. auth, code 41) emit the JSON doc on STDERR, not stdout.
        doc = _extract_json(stderr_tail) if stderr_tail.strip() else None
    if not isinstance(doc, dict):
        return {"ok": False, "status": "gemini_error",
                "error": f"gemini produced no parseable JSON (exit {rc})",
                **{**err_base, "stderr": stderr_tail or stdout[-1000:]}}

    session_id = doc.get("session_id") or resume_session_id
    response = (doc.get("response") or "").strip()
    stats = doc.get("stats") or {}
    err_obj = doc.get("error") or None

    tokens, usage_split = _sum_tokens(stats)
    calls, tool_lines = _tool_summary(stats)
    files = stats.get("files") or {}

    # The stats reveal which model actually served the run (the CLI may route through
    # a helper model too — pick the one that did the most work). Beats echoing the
    # requested model, which may have been empty (backend default).
    models_used = {m: int(((v or {}).get("tokens") or {}).get("total") or 0)
                   for m, v in (stats.get("models") or {}).items()}
    model_used = max(models_used, key=models_used.get) if models_used else (model or "")

    # json mode is final-only: synthesize ONE summary turn from the tool stats so the
    # turns/commands trace stays populated across backends. num_turns approximates
    # the tool-call count (Gemini doesn't expose model round-trips headlessly).
    num_turns = calls
    turns: list[dict[str, Any]] = []
    turn_log: list[str] = []
    if response or calls:
        turns.append({"turn": 1, "commands": tool_lines, "tools": tool_lines,
                      "text": response[:300], "tokens": tokens})
        turn_log.append(
            f"turn=1 toolCalls={calls} ok={stats.get('tools', {}).get('totalSuccess', 0)} "
            f"fail={stats.get('tools', {}).get('totalFail', 0)} "
            f"+{files.get('totalLinesAdded', 0)}/-{files.get('totalLinesRemoved', 0)} lines "
            f"tokens={tokens}"
        )

    structured = None
    error_msg = None
    if err_obj:
        error_msg = f"{err_obj.get('type', 'Error')}: {err_obj.get('message', '')}".strip()
        if err_obj.get("code") is not None:
            error_msg += f" (code {err_obj['code']})"

    ok = rc == 0 and not err_obj
    if ok and output_schema:
        structured = _extract_json(response)
        if structured is None and session_id:
            # One corrective retry, resuming the same session so context is kept.
            try:
                retry_args = [GEMINI_BIN, "-p",
                              "Your previous response was not valid JSON. Respond ONLY "
                              "with the single JSON object matching the schema — no "
                              "prose, no markdown fences.",
                              "-o", "json", "--skip-trust", "--approval-mode", "plan",
                              "--resume", str(session_id)]
                if model:
                    retry_args += ["-m", model]
                rc2, stdout2, _ = _run_once(retry_args, worktree, timeout_s)
                doc2 = _extract_json(stdout2) if stdout2.strip() else None
                if rc2 == 0 and isinstance(doc2, dict):
                    retry_resp = (doc2.get("response") or "").strip()
                    structured = _extract_json(retry_resp)
                    if structured is not None:
                        response = retry_resp
                    t2, _ = _sum_tokens(doc2.get("stats") or {})
                    tokens += t2
            except Exception as ex:  # noqa: BLE001
                log.warning("gemini structured retry failed: %s", ex)
        if structured is None:
            ok = False
            error_msg = error_msg or "structured output requested but response was not valid JSON"

    cost = price_usage(usage_split, model_used or "gemini") if tokens else 0.0

    if on_turn:
        try:
            on_turn({"status": "IN_PROGRESS", "turns": list(turns), "numTurns": num_turns,
                     "tokenUsed": tokens, "sessionId": session_id or ""})
        except Exception:  # noqa: BLE001
            pass

    return {
        "ok": ok,
        "status": "success" if ok else "gemini_error",
        "result": response,
        "structured": structured,
        "model": model_used,
        "session_id": session_id,
        "num_turns": num_turns,
        "turns": turns,
        "tokens": tokens,
        "cost_usd": round(cost, 6),
        "denials": [],
        "turn_log": turn_log,
        "error": error_msg if not ok else None,
        "stderr": stderr_tail,
    }
