#!/usr/bin/env python3
"""Prove production functions and workers contain no execution deadlines.

Polling long-waits and Textual notification display durations are explicitly
classified as non-aborting presentation/queue behavior. Everything capable of
terminating commands, HTTP requests, or orchestration is rejected.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOTS = (ROOT / "workers", ROOT / "tui", ROOT / "scripts")
DEADLINE_NAMES = {"timeout", "timeout_s", "timeout_seconds"}
# Waits that never abort running work, by (file, function): each one says why.
NON_ABORTING_WAITS = {
    ("workers/common/coding_agent.py", "_until_result"):
        "stops reading a session only after the agent handed in its result (its answer is kept); what the CLI was still "
        "waiting on is a command it moved to the background, which the session's cleanup stops",
}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _is_disabled_deadline(value: ast.AST) -> bool:
    if isinstance(value, ast.Constant) and value.value is None:
        return True
    return (
        isinstance(value, ast.Call)
        and _call_name(value.func).endswith("Timeout")
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Constant)
        and value.args[0].value is None
    )


def _worker_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not _call_name(decorator.func).endswith("worker_task"):
            continue
        for keyword in decorator.keywords:
            if keyword.arg == "task_definition_name" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
    return None


def audit() -> dict:
    violations: list[str] = []
    functions: dict[str, list[str]] = defaultdict(list)
    workers: list[dict[str, object]] = []
    python_files = 0

    for base in PYTHON_ROOTS:
        for path in sorted(base.rglob("*.py")):
            if ".venv" in path.parts or path == Path(__file__):
                continue
            python_files += 1
            relative = path.relative_to(ROOT).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            except SyntaxError as exc:
                violations.append(f"{relative}:{exc.lineno}: syntax error: {exc.msg}")
                continue
            exempt_lines = [(n.lineno, n.end_lineno or n.lineno) for n in ast.walk(tree)
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and (relative, n.name) in NON_ABORTING_WAITS]
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[relative].append(node.name)
                    worker = _worker_name(node)
                    if worker:
                        workers.append({"task": worker, "function": node.name,
                                        "source": relative, "line": node.lineno})
                    parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                    for parameter in parameters:
                        if parameter.arg.lower() in DEADLINE_NAMES:
                            violations.append(
                                f"{relative}:{parameter.lineno}: deadline parameter {parameter.arg} in {node.name}")
                elif isinstance(node, ast.Call):
                    name = _call_name(node.func)
                    if name.endswith("wait_for") and not any(
                            lo <= node.lineno <= hi for lo, hi in exempt_lines):
                        violations.append(f"{relative}:{node.lineno}: asyncio.wait_for aborts running work")
                    for keyword in node.keywords:
                        if (keyword.arg or "").lower() not in DEADLINE_NAMES:
                            continue
                        # Textual's notification timeout only controls how long a message is displayed.
                        if name.endswith(".notify") or name == "notify":
                            continue
                        # SDK batch-poll timeout is a non-aborting idle long-poll duration.
                        if name.endswith("batch_poll"):
                            continue
                        if not _is_disabled_deadline(keyword.value):
                            violations.append(
                                f"{relative}:{node.lineno}: active {keyword.arg} passed to {name or '<call>'}")
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and "TIMEOUT" in target.id.upper():
                            # Poll transport long-waits are non-aborting: an empty poll simply starts another poll.
                            if relative != "workers/common/polling.py":
                                violations.append(f"{relative}:{node.lineno}: timeout constant {target.id}")

    shell_pattern = re.compile(r"(?:^|\s)(?:timeout\s+\d|--max-time\b|--connect-timeout\b)")
    for path in sorted([ROOT / "run.sh", *(ROOT / "workers").rglob("*.sh"), *(ROOT / "scripts").rglob("*.sh")]):
        if not path.is_file():
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if shell_pattern.search(line):
                violations.append(f"{path.relative_to(ROOT)}:{line_number}: shell deadline: {line.strip()}")

    timeout_fields = 0
    for path in sorted((ROOT / "workers" / "workflows").rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        stack = [data]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"timeoutSeconds", "responseTimeoutSeconds", "pollTimeoutSeconds"}:
                        timeout_fields += 1
                        if child not in (0, None):
                            violations.append(
                                f"{path.relative_to(ROOT)}: nonzero Conductor {key}={child!r}")
                    stack.append(child)
            elif isinstance(value, list):
                stack.extend(value)

    return {
        "passed": not violations,
        "pythonFilesScanned": python_files,
        "functionsScanned": sum(len(names) for names in functions.values()),
        "functionModules": {name: sorted(names) for name, names in sorted(functions.items())},
        "workersScanned": sorted(workers, key=lambda item: (str(item["source"]), int(item["line"]))),
        "conductorTimeoutFieldsChecked": timeout_fields,
        "nonAbortingWaits": {f"{f}:{fn}": why for (f, fn), why in NON_ABORTING_WAITS.items()},
        "allowedNonAbortingControls": {
            "workers/common/polling.py": "Conductor batch-poll long-wait; empty response is repolled and no work is aborted",
            "tui notification calls": "Textual message display duration only; no process, request, task, or workflow is aborted",
            "workers/common/claude.py and tui/api.py": "HTTP client deadline is explicitly disabled with None",
        },
        # The one stop that is not a cancellation, by the person's rule (2026-11-15): a test run or check
        # that runs far past how long it has taken before is hung and is stopped to be investigated. Its
        # budget is a multiple of the command's own measured history, never a fixed deadline; a first run
        # has none (the progress rule applies), and TEST_BUDGET_S raises it for a suite that needs longer.
        "historyDerivedBudgets": {
            "workers/common/test_budget.py": "agents' test runs: BUDGET_FACTOR x their longest recent run + margin",
            "workers/common/check_execution.py execute(expected_s=)": "acceptance checks: 3x their longest recent run + 60 s",
        },
        "violations": violations,
    }


if __name__ == "__main__":
    result = audit()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    raise SystemExit(0 if result["passed"] else 1)
