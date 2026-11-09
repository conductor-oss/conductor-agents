"""Persistent, on-branch codebase knowledge (roadmap Phase 6, G5) — repo_memory with lesson decay.

Each run today re-derives repo understanding from scratch. ``repo_memory`` persists it **on the branch**
(a file later runs read natively): a module map, invariants, test conventions, and accumulated lessons.
It is the cross-run face of the learning board (§4.2).

The danger is H10/H15: a confidently-wrong lesson actively degrades planning. So every lesson carries
**provenance + confidence + refs**, and a lesson is **decayed/invalidated when the code it references
moves on** — anything it names (a file, or a ``path::symbol``) is verified to still exist before it is
trusted. Stale lessons
are never silently trusted; they are marked invalidated and dropped from the digest fed to planning.

Pure logic (fold/decay/digest) + thin file read/write; the ``@worker_task`` wrappers live in
``stages/tasks.py``.
"""

from __future__ import annotations

import json
import os
import time

from common import lesson_refs

MEMORY_PATH = ".conductor-code/repo_memory.json"


def _default() -> dict:
    return {"moduleMap": {}, "invariants": [], "conventions": {}, "lessons": []}


def _mapping(v):
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else {}


def _listed(v):
    return v if isinstance(v, list) else []


def read_memory(repo_path: str) -> dict:
    """Read the on-branch memory file, or an empty default when a repo has none yet (first run)."""
    path = os.path.join(str(repo_path), MEMORY_PATH)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {**_default(), **(data if isinstance(data, dict) else {})}
    except (OSError, ValueError):
        return _default()


def write_memory(repo_path: str, memory: dict) -> str:
    """Write the memory file (creating ``.conductor-code/``). Returns the absolute path written."""
    path = os.path.join(str(repo_path), MEMORY_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(memory, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def merge_lessons(memory: dict, new_lessons: list) -> dict:
    """Append lessons, de-duplicated by text; keep the highest-confidence/newest record for a repeat."""
    memory = {**_default(), **_mapping(memory)}
    by_text = {}
    rank = {"high": 2, "medium": 1, "low": 0}
    for les in _listed(memory.get("lessons")) + _listed(new_lessons):
        les = _mapping(les)
        text = str(les.get("text") or "").strip()
        if not text:
            continue
        rec = {"text": text, "confidence": str(les.get("confidence") or "medium"),
               "provenance": str(les.get("provenance") or ""),
               "refs": [str(r) for r in _listed(les.get("refs"))],
               "ts": les.get("ts") or time.time(),
               "invalidated": bool(les.get("invalidated", False))}
        prev = by_text.get(text)
        if prev is None or rank.get(rec["confidence"], 1) >= rank.get(prev["confidence"], 1):
            by_text[text] = rec
    memory["lessons"] = sorted(by_text.values(), key=lambda r: r["ts"])
    return memory


def decay_lessons(memory: dict, repo_path: str) -> dict:
    """Invalidate every lesson whose referenced files no longer exist (H10). A lesson with no refs is an
    unverifiable opinion — left as-is but never promoted. Returns ``{memory, invalidated, checked}``."""
    memory = {**_default(), **_mapping(memory)}
    lessons = []
    invalidated = 0
    for les in _listed(memory.get("lessons")):
        les = _mapping(les)
        refs = [str(r) for r in _listed(les.get("refs"))]
        # A ref is a path or ``path::symbol``: the file must exist and, for a symbol ref, still contain
        # the symbol (closure plan WP8) — a renamed function in a surviving file is stale too.
        stale = bool(lesson_refs.stale_refs(str(repo_path), refs))
        if stale and not les.get("invalidated"):
            invalidated += 1
        les = {**les, "invalidated": bool(les.get("invalidated")) or stale}
        lessons.append(les)
    memory["lessons"] = lessons
    return {"memory": memory, "invalidated": invalidated, "checked": len(lessons)}


def live_lessons(memory: dict) -> list:
    """Lessons safe to trust: not invalidated."""
    return [les for les in _listed(_mapping(memory).get("lessons")) if not _mapping(les).get("invalidated")]


def digest(memory: dict, *, limit: int = 12) -> str:
    """A prompt-ready digest of trustworthy repo knowledge for the next run's planner."""
    memory = _mapping(memory)
    lines = []
    invariants = [str(x) for x in _listed(memory.get("invariants")) if str(x).strip()]
    if invariants:
        lines.append("Invariants:")
        lines += [f"- {x}" for x in invariants[:limit]]
    live = live_lessons(memory)
    if live:
        lines.append("Lessons from prior runs (trust these; stale ones are already dropped):")
        for les in live[-limit:]:
            les = _mapping(les)
            refs = ", ".join(str(r) for r in _listed(les.get("refs")))
            lines.append(f"- [{les.get('confidence', 'medium')}] {les.get('text')}" + (f" (refs: {refs})" if refs else ""))
    return "\n".join(lines)
