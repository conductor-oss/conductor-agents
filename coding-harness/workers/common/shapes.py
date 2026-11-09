"""Conductor input/output shape coercion shared by the worker packages (closure plan WP10).

Conductor hands workers loosely-typed values (a list may arrive as a JSON string, a missing number as
"", a JOIN as ``{ref: {output: {...}}}``). These helpers normalize them once, here, instead of each
worker package keeping its own private copy or importing another package's privates.
"""

from __future__ import annotations

import json
import os
import re


def lines(value) -> list[str]:
    """Non-blank strings: a list's entries, or a text split on newlines with bullet/number markers
    stripped; a single-line text is one entry. A planner wrote each item's acceptance criteria as one
    paragraph, the board kept ``[]`` for a non-list, and no team of that run had a frozen test — an item
    whose work a sibling had absorbed then had no oracle to prove it done (run 90e5e3aa)."""
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        out = []
        for ln in value.splitlines():
            ln = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", ln).strip()
            if ln:
                out.append(ln)
        return out
    return []


def listed(value) -> list:
    """A list; a JSON-array string is parsed, a comma-separated string is split, anything else → []."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [x.strip() for x in value.split(",") if x.strip()]
        except ValueError:
            return [x.strip() for x in value.split(",") if x.strip()]
    return []


def mapping(value) -> dict:
    """A dict with string keys; a JSON-object string is parsed, anything else → {}."""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return mapping(parsed)
    return {}


def num(value):
    """A float, or None when blank/unparseable (a missing ceiling is 'unbounded', not a crash)."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def truth(value, default=True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "success", "completed")


def iter_joined(joined):
    """Flatten a FORK_JOIN_DYNAMIC's ``{ref: {output: {...}}}`` map to ``(ref, output)`` pairs."""
    for ref, value in mapping(joined).items():
        value = mapping(value)
        yield str(ref), (mapping(value.get("output")) or value)


def subtask_worktree(shared_repo: str, group_id: str) -> str:
    """Where code_subtask puts a fork's worktree: <sharedRepo>/.cc-worktrees/<id> on cc-group-<id>."""
    return os.path.join(shared_repo, ".cc-worktrees", group_id)
