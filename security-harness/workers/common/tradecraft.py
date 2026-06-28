"""Tradecraft as tunable DATA (§19 HC surface) — the exploitation technique ladders and the
injection-classifier signatures, versioned in ``catalog/tradecraft.yaml`` so hill-climbing can
propose ratify-gated edits (e.g. "add the SQLite error signature", "add a ladder rung").

Design: the authoritative defaults live in code (``deepen.py``, ``features.py``); this loader
OVERLAYS them with the YAML when it is present + non-empty. So behavior is unchanged at rest (the
YAML mirrors the constants — golden-tested), the system still works if the file is absent, and the
file becomes the lever HC tunes. Detection machinery → ratify-only (never auto-applied).
"""

from __future__ import annotations

import functools
import os

_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "catalog", "tradecraft.yaml")


@functools.lru_cache(maxsize=4)
def load(path: str | None = None) -> dict:
    """Parse the tradecraft data file. Returns {} on any error / absence (callers keep defaults).
    Cached; pass a path or set SC_TRADECRAFT to override (tests use distinct paths)."""
    p = path or os.environ.get("SC_TRADECRAFT") or _DEFAULT_PATH
    try:
        import yaml
        with open(p, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def ladders(default: dict) -> dict:
    """Overlay the YAML `ladders` onto the in-code default ladders (per-class). A class absent from
    the YAML keeps its default; the YAML can add classes or replace a class's rungs."""
    data = load().get("ladders")
    if not isinstance(data, dict) or not data:
        return default
    merged = dict(default)
    for cls, rungs in data.items():
        if isinstance(rungs, list) and rungs and all(isinstance(r, dict) and r.get("family") for r in rungs):
            merged[str(cls)] = rungs
    return merged


def signatures(key: str, default: tuple) -> tuple:
    """Overlay a YAML signature/keyword list (e.g. sql_signatures, file_signatures,
    common_query_params) onto the in-code default tuple. Non-list / empty → keep default."""
    data = load().get(key)
    if isinstance(data, list) and data:
        return tuple(str(x) for x in data)
    return default
