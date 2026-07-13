"""FileSessionStore — a shared-filesystem SessionStore for cross-host resume.

Session transcripts normally live on the host that ran the agent
(``~/.claude/projects/<encoded-cwd>/*.jsonl``), so when Conductor retries a task
carrying ``resumeSessionId`` on a DIFFERENT worker host, the transcript isn't
there and the resume silently starts fresh (docs/CLAUDE_AGENT_SDK.md §10 #1 bug).

A ``SessionStore`` closes that gap: the SDK still writes local disk first, but
mirrors each transcript batch to the store, and on resume hydrates from it. This
implementation persists to a directory — point every worker host at the SAME
shared path (NFS / a mounted volume) and any host can resume any session.

Enable it by setting ``CODING_AGENT_SESSION_STORE_DIR`` in the worker environment
(see ``coding_agent/tasks.py``). Off by default — single-host runs don't need it.

The SDK's ``SessionStore`` protocol requires only ``append`` and ``load``; we also
implement ``list_sessions`` and ``delete`` so session listing/cleanup work too.
The methods are ``async`` — the SDK awaits them — so the (blocking) file I/O runs
off the event loop via ``asyncio.to_thread``. Entries are opaque transcript records
(dicts); we persist them verbatim as JSON lines so the store is agnostic to shape.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

# SessionKey is a TypedDict {project_key, session_id, subpath?}; treat as a dict.
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class FileSessionStore:
    """Persist session transcripts under ``base_dir`` on a shared filesystem."""

    def __init__(self, base_dir: str) -> None:
        self.base_dir = os.path.realpath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, key: dict[str, Any]) -> str:
        # project_key is already fs-safe (e.g. "-private-tmp-hello-go"); session_id
        # is a uuid; subpath (subagent transcripts) may contain separators — sanitize.
        project = _SAFE.sub("_", str(key.get("project_key") or "default"))
        session = _SAFE.sub("_", str(key.get("session_id") or "unknown"))
        sub = _SAFE.sub("_", str(key.get("subpath") or "root"))
        d = os.path.join(self.base_dir, project, session)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{sub}.jsonl")

    # --- protocol methods (async: the SDK awaits them) ------------------------
    async def append(self, key: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        if entries:
            await asyncio.to_thread(self._append_sync, key, entries)

    async def load(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        return await asyncio.to_thread(self._load_sync, key)

    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_sync, project_key)

    async def delete(self, key: dict[str, Any]) -> None:
        await asyncio.to_thread(self._delete_sync, key)

    # --- blocking implementations (run off the event loop) --------------------
    def _append_sync(self, key: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        path = self._path(key)
        with open(path, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def _load_sync(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        path = self._path(key)
        if not os.path.exists(path):
            return None
        out: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def _list_sync(self, project_key: str) -> list[dict[str, Any]]:
        project = _SAFE.sub("_", str(project_key or "default"))
        pdir = os.path.join(self.base_dir, project)
        if not os.path.isdir(pdir):
            return []
        entries: list[dict[str, Any]] = []
        for session_id in os.listdir(pdir):
            sdir = os.path.join(pdir, session_id)
            if not os.path.isdir(sdir):
                continue
            try:
                mtime = int(os.path.getmtime(sdir) * 1000)
            except OSError:
                mtime = 0
            entries.append({"session_id": session_id, "mtime": mtime})
        return entries

    def _delete_sync(self, key: dict[str, Any]) -> None:
        try:
            os.remove(self._path(key))
        except OSError:
            pass


def store_from_env() -> FileSessionStore | None:
    """Construct a FileSessionStore if CODING_AGENT_SESSION_STORE_DIR is set."""
    base = os.environ.get("CODING_AGENT_SESSION_STORE_DIR")
    return FileSessionStore(base) if base else None
