"""Optional `gh` integration for the launcher's issue/PR pickers.

Everything degrades gracefully: no `gh`, not authenticated, or any error → returns
None and the launcher falls back to a plain number input.
"""

from __future__ import annotations

import asyncio
import json
import shutil

from .catalog import short_repo


def available() -> bool:
    return shutil.which("gh") is not None


async def _list(repo: str, kind: str) -> list[tuple[int, str]] | None:
    """kind = 'issue' | 'pr'. Returns [(number, title)] or None on any failure."""
    if not available() or not repo.strip():
        return None
    slug = short_repo(repo)
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", kind, "list", "--repo", slug, "--json", "number,title",
            "--limit", "30", "--state", "open",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return None
        data = json.loads(out.decode() or "[]")
        return [(int(x["number"]), str(x.get("title", ""))) for x in data]
    except Exception:  # noqa: BLE001
        return None


async def list_issues(repo: str):
    return await _list(repo, "issue")


async def list_prs(repo: str):
    return await _list(repo, "pr")
