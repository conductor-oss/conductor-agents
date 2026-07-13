"""Standalone smoke test for the coding_agent worker — no Conductor server needed.

Drives ``run_coding_agent`` directly against a throwaway temp worktree, asking it
to implement "hello world". Per docs/CLAUDE_AGENT_SDK.md a single-file write is one
turn (one tool-use round trip), so this should finish in ~1 turn.

Run:  ANTHROPIC_API_KEY=... .venv/bin/python -m coding_agent.smoke_test
(from the workers/ directory, so `common` is importable).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

from common.coding_agent import run_coding_agent

PROMPT = (
    "Create a single file named hello.py in the current directory that prints "
    "exactly `Hello, World!` when run with `python hello.py`. Just write that one "
    "file — do not run it, test it, or take any other action."
)


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("SKIP: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="coding_agent_smoke_") as wt:
        before = set(os.listdir(wt))
        print(f"[smoke] worktree={wt}")
        print(f"[smoke] prompt={PROMPT!r}\n")

        # run_coding_agent is async (the worker awaits it on the AsyncTaskRunner
        # loop); this standalone script owns its own loop, so asyncio.run is right.
        res = asyncio.run(run_coding_agent(
            PROMPT,
            worktree=wt,
            max_turns=10,
            max_budget_usd=1.0,
            timeout_s=180,
        ))

        print("[smoke] --- result ---")
        for k in ("ok", "status", "num_turns", "tokens", "cost_usd", "session_id", "error"):
            print(f"  {k}: {res.get(k)}")
        for entry in res.get("turn_log") or []:
            print(f"  turn_log: {entry}")
        for d in res.get("denials") or []:
            print(f"  DENIED: {d}")
        if res.get("result"):
            print(f"  agent said: {res['result'][:400]}")

        after = set(os.listdir(wt))
        new_files = sorted(after - before)
        print(f"\n[smoke] new files: {new_files}")
        hello = os.path.join(wt, "hello.py")
        contents = ""
        if os.path.exists(hello):
            with open(hello, encoding="utf-8") as f:
                contents = f.read()
            print(f"[smoke] hello.py:\n{'-' * 40}\n{contents}{'-' * 40}")

        # Assertions
        problems = []
        if not res.get("ok"):
            problems.append(f"status not ok: {res.get('status')} / {res.get('error')}")
        if not os.path.exists(hello):
            problems.append("hello.py was not created")
        elif "Hello, World!" not in contents:
            problems.append("hello.py does not print 'Hello, World!'")
        turns = res.get("num_turns") or 0
        # "One turn" is the target; allow a small margin (a stray Read) but flag >2.
        if turns > 2:
            problems.append(f"took {turns} turns (expected ~1)")

        if problems:
            print("\n[smoke] FAIL:")
            for p in problems:
                print(f"  - {p}")
            return 1

        print(f"\n[smoke] PASS — hello.py written in {turns} turn(s), "
              f"${res.get('cost_usd', 0):.4f}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
