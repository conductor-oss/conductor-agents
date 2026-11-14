"""Replay the spec's trace suite against the program through its adapter (docs/FORMAL_SPEC_DESIGN.md).

The adapter is a long-running program speaking JSON lines on stdin/stdout:

    {"op": "reset"}                                    -> {"ok": true}
    {"op": "step", "action": "Create", "args": [...]}  -> {"ok": true, "result": <observable result>}
    {"op": "state"}                                    -> {"ok": true, "state": {<spec variable>: <value>}}

After every step the program's result must equal the spec's ``result`` variable and its abstract state
must equal the spec's ``state`` variables. Every trace is the shortest path to one transition of the
bounded model, so a mismatch is reported with the shortest explanation there is.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time

MAX_REPORTED = 8


def _order(v):
    return json.dumps(v, sort_keys=True, default=str)


def _is_set(v) -> bool:
    return isinstance(v, dict) and set(v) == {"$set"}


def canon(v):
    """A spec value in comparable form: a set becomes a sorted list (sets are unordered)."""
    if _is_set(v):
        return sorted((canon(x) for x in v["$set"]), key=_order)
    if isinstance(v, dict):
        return {k: canon(x) for k, x in v.items()}
    if isinstance(v, list):
        return [canon(x) for x in v]
    return v


def shape(expected, actual):
    """The program's value in comparable form, shaped by the spec's: where the spec has a set, the
    program's list is compared unordered; an empty TLA+ function (printed ``<<>>``) equals ``{}``."""
    if canon(expected) in ([], {}) and actual in ([], {}, None):
        return canon(expected)
    if _is_set(expected) and isinstance(actual, list):
        template = expected["$set"][0] if expected["$set"] else None
        return sorted((shape(template, a) for a in actual), key=_order)
    if isinstance(expected, dict) and not _is_set(expected) and isinstance(actual, dict):
        return {k: shape(expected.get(k), v) for k, v in actual.items()}
    if isinstance(expected, list) and isinstance(actual, list):
        return [shape(expected[i] if i < len(expected) else (expected[0] if expected else None), a)
                for i, a in enumerate(actual)]
    return actual


def normalize(expected, actual) -> tuple:
    return canon(expected), shape(expected, actual)


class Adapter:
    """The adapter process, one request at a time."""

    def __init__(self, command: str, cwd: str, env: dict):
        # A long-running, interactive process (one JSON line in, one out, for every step of every trace),
        # so a Popen with the shared runtime environment rather than a one-shot command.
        from common import check_execution
        self.proc = subprocess.Popen(shlex.split(command), cwd=cwd, env=check_execution.inherited_environment(env),
                                     stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     start_new_session=True)
        self.stderr: list[str] = []
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        for line in self.proc.stderr:
            self.stderr.append(line)
            del self.stderr[:-200]

    def call(self, msg: dict) -> dict:
        if self.proc.poll() is not None:
            raise RuntimeError(f"the adapter exited (code {self.proc.returncode}): {''.join(self.stderr)[-1500:]}")
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"the adapter closed its output: {''.join(self.stderr)[-1500:]}")
        try:
            return json.loads(line)
        except ValueError:
            raise RuntimeError(f"the adapter answered something that is not JSON: {line[:300]!r}")

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(10)
        except Exception:  # noqa: BLE001 - it is going away either way
            pass
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, 15)
            except OSError:
                pass


def run(workdir: str, *, env: dict | None = None) -> dict:
    """Replay every trace of ``.cc/conformance/traces.json``. ``{"ok", "traces", "passed", "failed",
    "mismatches": [...], "summary", "seconds"}``."""
    path = os.path.join(workdir, ".cc", "conformance", "traces.json")
    try:
        with open(path, encoding="utf-8") as fh:
            suite = json.load(fh)
    except (OSError, ValueError) as e:
        return {"ok": False, "summary": f"no trace suite: {e}", "traces": 0, "passed": 0, "failed": 0, "mismatches": []}
    state_vars, result_var = list(suite.get("state") or []), str(suite.get("result") or "")
    command = str(suite.get("adapter") or "")
    if not command:
        return {"ok": False, "summary": "the spec names no adapter (abstraction.json \"adapter\")",
                "traces": 0, "passed": 0, "failed": 0, "mismatches": []}
    started = time.monotonic()
    adapter = Adapter(command, workdir, env or {})
    passed, mismatches, seen = 0, [], set()
    try:
        for n, trace in enumerate(suite.get("traces") or []):
            failure = _replay(adapter, trace, state_vars, result_var)
            if failure is None:
                passed += 1
                continue
            key = (failure["action"], failure["what"])
            if key not in seen and len(mismatches) < MAX_REPORTED:
                seen.add(key)
                mismatches.append({**failure, "trace": n})
            if failure.get("fatal"):
                break
    finally:
        adapter.close()
    total = len(suite.get("traces") or [])
    failed = total - passed
    summary = (f"{passed} of {total} traces conform (every transition of the bounded model)" if not failed else
               f"{failed} of {total} traces diverge from the spec; first: " + _describe(mismatches[0]) if mismatches
               else f"{failed} of {total} traces diverge")
    return {"ok": failed == 0 and total > 0, "traces": total, "passed": passed, "failed": failed,
            "mismatches": mismatches, "summary": summary, "seconds": round(time.monotonic() - started, 1)}


def _replay(adapter: Adapter, trace: dict, state_vars: list, result_var: str) -> dict | None:
    try:
        r = adapter.call({"op": "reset"})
        if not r.get("ok", False):
            return {"step": 0, "action": "reset", "what": "reset", "expected": "ok", "actual": r, "path": [], "fatal": True}
        path = []
        for i, step in enumerate(trace["steps"]):
            path.append(f"{step['action']}({', '.join(json.dumps(a) for a in step['args'])})")
            got = adapter.call({"op": "step", "action": step["action"], "args": step["args"]})
            if result_var:
                exp, act = normalize(step["state"].get(result_var), got.get("result"))
                if exp != act:
                    return {"step": i + 1, "action": step["action"], "what": "result", "expected": exp, "actual": act, "path": path}
            if state_vars:
                st = adapter.call({"op": "state"}).get("state") or {}
                for var in state_vars:
                    exp, act = normalize(step["state"].get(var), st.get(var))
                    if exp != act:
                        return {"step": i + 1, "action": step["action"], "what": f"state {var}",
                                "expected": exp, "actual": act, "path": path}
        return None
    except RuntimeError as e:
        return {"step": 0, "action": "adapter", "what": "adapter", "expected": "a running adapter", "actual": str(e),
                "path": [], "fatal": True}


def _describe(m: dict) -> str:
    return (f"after {' → '.join(m['path']) or '(start)'}: {m['what']} expected {json.dumps(m['expected'])[:300]}, "
            f"the program gave {json.dumps(m['actual'])[:300]}")


def report(result: dict) -> str:
    """The conformance result as check output for the measurement and the architect."""
    lines = [result.get("summary") or ""]
    for m in result.get("mismatches") or []:
        lines.append("- " + _describe(m))
    return "\n".join(lines)
