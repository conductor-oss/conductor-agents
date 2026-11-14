"""Subprocess helper that runs until completion or explicit cancellation."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .check_execution import execute, inherited_environment


@dataclass
class RunResult:
    stdout: str
    stderr: str
    code: int


class RunError(RuntimeError):
    def __init__(self, cmd: str, code: int, stdout: str, stderr: str):
        super().__init__(f"{cmd} exited {code}: {(stderr or stdout)[:300]}")
        self.code = code
        self.stdout = stdout
        self.stderr = stderr


def run(cmd: list[str], cwd: str | None = None, check: bool = True,
        env: dict[str, str] | None = None, clean_env: bool = False) -> RunResult:
    """Run a command with stdin closed. Raises RunError on non-zero exit when
    ``check`` is True; otherwise returns the RunResult regardless of code."""
    # Every harness command gets process-group ownership. ``clean_env`` is retained for callers such as
    # exact-SHA verification that provide a complete reproducible environment.
    execution_env = dict(env or {}) if clean_env else inherited_environment(env)
    # The harness parses what these commands print (git status, git diff --name-only, gh api), so the
    # output is kept whole. The check runner's 1 MB tail cap once cut a 2.8 MB `git status` of a tree
    # holding an unignored node_modules: every backend path sorted before the cut, the commit staged
    # nothing, the audit called six written files missing, and eight agents redid done work (run a87e9ddb).
    result = execute(cmd, cwd=cwd or os.getcwd(), env=execution_env, capture_limit=None, reap_escaped=False)
    res = RunResult(stdout=result.stdout, stderr=result.stderr, code=result.exit_code)
    if check and res.code != 0:
        raise RunError(" ".join(cmd), res.code, res.stdout, res.stderr)
    return res
