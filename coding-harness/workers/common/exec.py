"""Subprocess helper: stdin closed, captured stdout/stderr, and non-zero exits
raise with output attached. Runtime deadlines are owned by Conductor task defs."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


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


def run(cmd: list[str], cwd: str | None = None, check: bool = True) -> RunResult:
    """Run a command with stdin closed. Raises RunError on non-zero exit when
    ``check`` is True; otherwise returns the RunResult regardless of code."""
    proc = subprocess.run(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
    )
    res = RunResult(stdout=proc.stdout or "", stderr=proc.stderr or "", code=proc.returncode)
    if check and proc.returncode != 0:
        raise RunError(" ".join(cmd), proc.returncode, res.stdout, res.stderr)
    return res
