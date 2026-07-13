"""Run the canonical workflow-registration script without blocking Textual."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RegistrationResult:
    ok: bool
    output: str


def script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "workers" / "register.sh"


async def register_definitions(server_url: str, timeout_s: float = 180.0) -> RegistrationResult:
    """Register task/workflow definitions and run the worker gate.

    The script remains the single source of truth for ordering and validation. The
    selected TUI server overrides any inherited CONDUCTOR_SERVER_URL so `--server`
    behaves consistently for registration and normal TUI API calls.
    """
    script = script_path()
    if not script.is_file():
        return RegistrationResult(False, f"registration script not found: {script}")

    env = os.environ.copy()
    env["CONDUCTOR_SERVER_URL"] = server_url
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", str(script),
            cwd=str(script.parent), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return RegistrationResult(False, f"registration timed out after {timeout_s:.0f}s")
    except OSError as exc:
        return RegistrationResult(False, f"could not start registration: {exc}")

    output = stdout.decode(errors="replace").strip()
    return RegistrationResult(proc.returncode == 0, output)
