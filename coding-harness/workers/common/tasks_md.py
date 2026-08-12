"""Deterministic parser: OpenSpec tasks.md -> candidate parallel subtasks.

This parser handles document syntax only.  The single plan validator owns
cross-task safety checks such as duplicate IDs and overlapping file claims.
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$")
_CHECKBOX_RE = re.compile(r"^-\s*\[[ xX]\]\s*(.+?)\s*$")
_FILES_RE = re.compile(r"^Files:\s*(.+?)\s*$", re.IGNORECASE)


class TasksMdError(ValueError):
    """Raised when tasks.md violates the independent, file-disjoint group contract."""


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "group"


def parse_tasks_md(text: str) -> list[dict]:
    """Split tasks.md on `## N. <title>` headings into independent subtask
    groups. Each group must declare exact files and at least one checkbox task.
    Test commands are intentionally ignored; planning and testing are separate."""
    groups: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if current is not None:
                groups.append(current)
            current = {"title": m.group(2), "files": None, "bullets": []}
            continue
        if current is None:
            continue
        fm = _FILES_RE.match(line)
        if fm and current["files"] is None:
            current["files"] = [f.strip() for f in fm.group(1).split(",") if f.strip()]
            continue
        cm = _CHECKBOX_RE.match(line)
        if cm:
            current["bullets"].append(cm.group(1).strip())
    if current is not None:
        groups.append(current)

    if not groups:
        raise TasksMdError("tasks.md has no `## N. <title>` groups to parse")

    seen_ids: dict[str, int] = {}
    subtasks = []
    for g in groups:
        if not g["files"]:
            raise TasksMdError(f"group '{g['title']}' is missing a `Files:` line")
        if not g["bullets"]:
            raise TasksMdError(f"group '{g['title']}' has no `- [ ]` checkbox tasks")
        base = _slugify(g["title"])
        seen_ids[base] = seen_ids.get(base, 0) + 1
        sid = base if seen_ids[base] == 1 else f"{base}-{seen_ids[base]}"
        subtasks.append({
            "id": sid,
            "description": "\n".join(g["bullets"]),
            "files": g["files"],
        })
    return subtasks
