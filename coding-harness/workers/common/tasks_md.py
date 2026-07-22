"""Deterministic parser: OpenSpec tasks.md -> the subtasks[] shape code_parallel's
FORK_JOIN_DYNAMIC fan-out expects ({id, description, files, testCmd}). tasks.md
groups must be independent and file-disjoint (openspec_cli.TASKS_RULE enforces
this at generation time); this parser enforces it again at parse time and fails
closed on any violation rather than silently dropping it.
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$")
_CHECKBOX_RE = re.compile(r"^-\s*\[[ xX]\]\s*(.+?)\s*$")
_FILES_RE = re.compile(r"^Files:\s*(.+?)\s*$", re.IGNORECASE)
_TEST_RE = re.compile(r"^Test:\s*(.+?)\s*$", re.IGNORECASE)


class TasksMdError(ValueError):
    """Raised when tasks.md violates the independent, file-disjoint group contract."""


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "group"


def parse_tasks_md(text: str) -> list[dict]:
    """Split tasks.md on `## N. <title>` headings into independent subtask
    groups. Each group MUST declare a `Files:` line and a `Test:` line; no file
    may repeat across groups. Raises TasksMdError on any violation."""
    groups: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if current is not None:
                groups.append(current)
            current = {"title": m.group(2), "files": None, "test": None, "bullets": []}
            continue
        if current is None:
            continue
        fm = _FILES_RE.match(line)
        if fm and current["files"] is None:
            current["files"] = [f.strip() for f in fm.group(1).split(",") if f.strip()]
            continue
        tm = _TEST_RE.match(line)
        if tm and current["test"] is None:
            current["test"] = tm.group(1).strip()
            continue
        cm = _CHECKBOX_RE.match(line)
        if cm:
            current["bullets"].append(cm.group(1).strip())
    if current is not None:
        groups.append(current)

    if not groups:
        raise TasksMdError("tasks.md has no `## N. <title>` groups to parse")

    seen_ids: dict[str, int] = {}
    seen_files: dict[str, str] = {}
    subtasks = []
    for g in groups:
        if not g["files"]:
            raise TasksMdError(f"group '{g['title']}' is missing a `Files:` line")
        if not g["test"]:
            raise TasksMdError(f"group '{g['title']}' is missing a `Test:` line")
        if not g["bullets"]:
            raise TasksMdError(f"group '{g['title']}' has no `- [ ]` checkbox tasks")
        for f in g["files"]:
            if f in seen_files:
                raise TasksMdError(
                    f"file '{f}' is claimed by both group '{seen_files[f]}' and "
                    f"group '{g['title']}' — tasks.md groups must be file-disjoint"
                )
            seen_files[f] = g["title"]
        base = _slugify(g["title"])
        seen_ids[base] = seen_ids.get(base, 0) + 1
        sid = base if seen_ids[base] == 1 else f"{base}-{seen_ids[base]}"
        subtasks.append({
            "id": sid,
            "description": "\n".join(g["bullets"]),
            "files": g["files"],
            "testCmd": g["test"],
        })
    return subtasks
