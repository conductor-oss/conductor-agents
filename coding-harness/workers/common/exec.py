"""Subprocess helper: stdin closed, captured stdout/stderr, and non-zero exits
raise with output attached. Runtime deadlines are owned by Conductor task defs."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

# Homebrew/uv/cargo install here, but a worker started by a restart-loop wrapper,
# launchd, or any other non-login-shell context won't have them on PATH (macOS
# defaults bare processes to /usr/bin:/bin:/usr/sbin:/sbin). Appended — never
# overriding the inherited PATH — and only when the dir exists and isn't already
# on it, so a correctly configured PATH is untouched.
_EXTRA_PATH_DIRS = (
    "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
    os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.cargo/bin"),
)


def _augmented_env() -> dict[str, str]:
    env = os.environ.copy()
    parts = env.get("PATH", "").split(os.pathsep)
    seen = set(parts)
    extra = [d for d in _EXTRA_PATH_DIRS if d not in seen and os.path.isdir(d)]
    if extra:
        env["PATH"] = os.pathsep.join([*parts, *extra])
    return env


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
        capture_output=True, text=True, env=_augmented_env(),
    )
    res = RunResult(stdout=proc.stdout or "", stderr=proc.stderr or "", code=proc.returncode)
    if check and proc.returncode != 0:
        raise RunError(" ".join(cmd), proc.returncode, res.stdout, res.stderr)
    return res
