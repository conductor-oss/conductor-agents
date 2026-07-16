"""Pre-merge verification gate worker: ``run_checks``.

A thin ``@worker_task`` over ``common/exec.py`` that runs a repo's test/build
command and reports pass/fail *as data*, never as a task failure. A red suite is
a COMPLETED task with ``passed=False`` — the workflow branches on the output, so
verification is fail-soft. ``fail()`` is reserved for genuine worker errors
(missing repoPath, not a directory, bash failing to spawn).

Command resolution: an explicit ``cmd`` wins; an empty ``cmd`` falls back to a
best-effort ``_detect_default`` file probe; if neither yields a command, nothing
runs and the check is a fail-open skip (``passed=True``).
"""

from __future__ import annotations

import json as _json
import re
import time
from pathlib import Path

from conductor.client.worker.worker_task import worker_task

from common.exec import run
from common.results import cap, fail, ok

OUTPUT_CAP = 4000


def _detect_default(repo_path: str) -> str | None:
    """Best-effort ORDERED probe for a repo's default check command (first match
    wins). File reads only — no subprocess, no network. Returns ``None`` when no
    known marker is present."""
    root = Path(repo_path)

    if any((root / f).exists() for f in
           ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")):
        return "pytest -q"

    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = _json.loads(pkg.read_text())
            if data.get("scripts", {}).get("test"):
                return "npm test --silent"
        except Exception:  # noqa: BLE001 — malformed JSON => no test script
            pass

    makefile = root / "Makefile"
    if makefile.exists():
        try:
            if re.search(r"^test:", makefile.read_text(), re.MULTILINE):
                return "make test"
        except Exception:  # noqa: BLE001
            pass

    if (root / "go.mod").exists():
        return "go test ./..."

    if (root / "Cargo.toml").exists():
        return "cargo test"

    return None


@worker_task(task_definition_name="run_checks")
def run_checks(task):
    """Run a repo's test/build command as a pre-merge gate. Executes the supplied
    ``cmd`` (or a detected default) and reports pass/fail as output data — a
    failing check is COMPLETED, never FAILED."""
    i = task.input_data or {}
    repo_path = (i.get("repoPath") or "").strip()
    if not repo_path or not Path(repo_path).is_dir():
        return fail(task, "run_checks",
                    f"repoPath missing or not a directory: {repo_path!r}")

    cmd = (i.get("cmd") or "").strip()
    label = (i.get("label") or "").strip()

    detected_cmd = _detect_default(repo_path) if not cmd else None
    effective_cmd = cmd or detected_cmd

    if not effective_cmd:
        # Nothing to run — fail-open skip.
        output = {
            "cmd": "",
            "ran": False,
            "detected": False,
            "passed": True,
            "exitCode": None,
            "stdout": "",
            "stderr": "",
            "durationMs": 0,
            "summary": "",
        }
    else:
        start = time.monotonic()
        try:
            result = run(["bash", "-c", effective_cmd], cwd=repo_path, check=False)
        except Exception as e:  # noqa: BLE001 — bash failed to spawn: real error
            return fail(task, "run_checks", e)
        duration_ms = int((time.monotonic() - start) * 1000)
        output = {
            "cmd": effective_cmd,
            "ran": True,
            "detected": (not cmd and detected_cmd is not None),
            "passed": result.code == 0,
            "exitCode": result.code,
            "stdout": cap(result.stdout, OUTPUT_CAP),
            "stderr": cap(result.stderr, OUTPUT_CAP),
            "durationMs": duration_ms,
            "summary": "",
        }

    if output["ran"]:
        state = "pass" if output["passed"] else "fail"
    else:
        state = "skip"
    code_or_dash = output["exitCode"] if output["exitCode"] is not None else "-"
    tag = f"[{label}]" if label else ""
    summary = (f"run_checks{tag}: {state} (exit {code_or_dash}) "
               f"cmd=`{effective_cmd or '(none)'}`")
    output["summary"] = summary

    return ok(task, output, [summary])
