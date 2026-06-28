"""Optional TARGET profiles.

The harness engine is generic — it makes no assumptions about what the target is.
A *profile* is an OPT-IN bundle of target-platform hints (auth scheme + protected
probe paths, the key/secret token-exchange body shape, and cleanup resource families)
that sharpens testing/cleanup against a known platform WITHOUT putting that knowledge
in the engine. No profile selected -> fully generic behavior.

A profile is plain JSON under ``profiles/`` (or an absolute path), loaded by name.
"""

from __future__ import annotations

import json
import os


def profiles_dir() -> str:
    # repo_root/profiles  (this file is repo_root/workers/common/profiles.py)
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(here))
    return os.environ.get("SC_PROFILES_DIR", os.path.join(repo, "profiles"))


def load(name_or_path: str | None) -> dict:
    """Load a profile by name (``profiles/<name>.json``) or absolute path. Returns {}
    when nothing is selected or the file is missing/invalid (engine stays generic)."""
    if not name_or_path:
        return {}
    path = name_or_path if os.path.isfile(name_or_path) \
        else os.path.join(profiles_dir(), f"{name_or_path}.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
