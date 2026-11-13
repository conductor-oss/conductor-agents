"""Prompts handed to dynamically forked tasks travel as files, not as task-input strings.

Conductor scans every literal string in a scheduled task's input parameters for ``${...}`` and
substitutes each as an expression; a value it cannot resolve becomes ``null``. A dynamic fork's
``dynamicTasksInput`` IS the forked task's input parameters, so a reviewer's prompt that quoted the
diff of a JavaScript file lost every template literal: ``\`M${x},${y}\``` reached the reviewer as
``Mnull,null`` and ``viewBox={\`0 0 ${width} ${height}\`}`` as ``0 0 null null``. The reviewer
reported both as bugs in correct code and the item went back for a rework round in two consecutive
runs (806e18ba, 7213a8d0). A path carries no ``${``; the worker reads the prompt from the file.
"""
from __future__ import annotations

import hashlib
import json
import os

PROMPTS_DIR = os.path.join(".cc-cache", "prompts")


def stash(root: str, ref: str, prompt: str, context: dict | None = None) -> str:
    """Write ``{prompt, promptContext}`` under ``<root>/.cc-cache/prompts/`` and return the path; ""
    when there is no root to write under (the caller then sends the prompt inline as before)."""
    if not root or not os.path.isdir(root):
        return ""
    body = json.dumps({"prompt": str(prompt or ""), "promptContext": dict(context or {})}, ensure_ascii=False)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:10]
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(ref or "prompt"))[:80]
    folder = os.path.join(root, PROMPTS_DIR)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{safe}-{digest}.json")
    if not os.path.isfile(path):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    return path


def load(path: str) -> tuple[str, dict]:
    """The stashed prompt and its context; ("", {}) when the path is missing or unreadable."""
    try:
        with open(str(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return "", {}
    if not isinstance(data, dict):
        return "", {}
    ctx = data.get("promptContext")
    return str(data.get("prompt") or ""), (ctx if isinstance(ctx, dict) else {})
