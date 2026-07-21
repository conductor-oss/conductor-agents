"""Read-only local workspace preview used by chat and launcher confirmations."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspacePreview:
    source: str
    planned: str
    ignored_changes: int
    ignored_paths: tuple[str, ...]


def preview(repo_path: str) -> WorkspacePreview | None:
    if not str(repo_path or "").strip():
        return None
    source = Path(repo_path).expanduser().resolve(strict=False)
    if source.is_dir():
        try:
            root = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if root.returncode == 0 and root.stdout.strip():
                source = Path(root.stdout.strip()).resolve()
        except (OSError, subprocess.TimeoutExpired):
            pass
    planned = source / ".cc-worktrees" / "run-<workflow-id>"
    paths: list[str] = []
    if source.is_dir():
        try:
            proc = subprocess.run(
                ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if proc.returncode == 0:
                paths = [
                    line[3:].strip()
                    for line in proc.stdout.splitlines()
                    if line.strip() and not line[3:].strip().startswith(".cc-worktrees/")
                ]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return WorkspacePreview(str(source), str(planned), len(paths), tuple(paths))
