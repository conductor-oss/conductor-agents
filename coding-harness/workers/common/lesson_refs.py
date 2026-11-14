"""Verifying what a lesson names (roadmap H10/H15, closure plan WP8).

A lesson is trusted only while everything it references still exists. A ref is a repository-relative
path, or ``path::symbol`` — then the file must exist AND still contain that symbol as a whole word.
Symbol-level refs catch the staleness file-level decay misses: a renamed function in a file that still
exists. Language-agnostic by design (a word-boundary match, no parser).
"""

from __future__ import annotations

import os
import re


def verify(repo: str, ref: str) -> bool:
    ref = str(ref or "").strip()
    if not ref:
        return True
    path, _, symbol = ref.partition("::")
    full = os.path.join(str(repo), path)
    if not os.path.exists(full):
        return False
    if not symbol:
        return True
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            return re.search(r"(?<![A-Za-z0-9_])" + re.escape(symbol) + r"(?![A-Za-z0-9_])", fh.read()) is not None
    except OSError:
        return False


def stale_refs(repo: str, refs) -> list[str]:
    return [str(r) for r in (refs or []) if not verify(repo, str(r))]
