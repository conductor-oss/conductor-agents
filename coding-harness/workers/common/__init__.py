"""Shared helpers for the coding-harness workers."""

import os as _os
import sys as _sys


def _ensure_swarm_substrate_on_path() -> None:
    """Add the repo root to ``sys.path`` so the shared ``swarm_substrate`` package imports.

    Walks upward from this file until it finds a directory containing ``swarm_substrate/`` (depth-
    independent, so it survives moves). No-op if already resolvable. A deployment that ships only
    ``workers/`` (e.g. a slim Docker image) must also copy ``swarm_substrate/`` for this to find it."""
    d = _os.path.dirname(_os.path.abspath(__file__))
    for _ in range(6):
        d = _os.path.dirname(d)
        if _os.path.isdir(_os.path.join(d, "swarm_substrate")):
            if d not in _sys.path:
                _sys.path.insert(0, d)
            return


_ensure_swarm_substrate_on_path()
