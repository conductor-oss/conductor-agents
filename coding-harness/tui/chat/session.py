"""Chat session persistence.

A session is the full conversation (Anthropic message blocks, including tool_use /
tool_result so context replays on resume) plus the model and the workflow ids launched
in it. Stored one JSON per session under ``~/.conductor-harness/sessions/`` (override
with ``$CONDUCTOR_HARNESS_HOME``).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path


def sessions_dir() -> Path:
    home = os.environ.get("CONDUCTOR_HARNESS_HOME")
    base = Path(home) if home else Path.home() / ".conductor-harness"
    d = base / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:40] or "session")


@dataclass
class Session:
    id: str
    model: str
    created: float
    updated: float
    title: str = "New session"
    messages: list = field(default_factory=list)   # Anthropic message dicts
    runs: list = field(default_factory=list)        # workflow ids started this session

    @classmethod
    def new(cls, model: str) -> "Session":
        ts = time.time()
        # stable, sortable id; slug filled in from the first user message later
        sid = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
        return cls(id=sid, model=model, created=ts, updated=ts)

    def set_title_from(self, text: str) -> None:
        if self.title == "New session" and text.strip():
            self.title = text.strip()[:60]

    def add_run(self, workflow_id: str) -> None:
        if workflow_id and workflow_id not in self.runs:
            self.runs.append(workflow_id)


class SessionStore:
    def __init__(self, directory: Path | None = None):
        self.dir = directory or sessions_dir()

    def _path(self, sid: str) -> Path:
        return self.dir / f"{sid}.json"

    def save(self, s: Session) -> None:
        s.updated = time.time()
        path = self._path(s.id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(s), indent=1))
        tmp.replace(path)          # atomic

    def load(self, sid: str) -> Session | None:
        p = self._path(sid)
        if not p.exists():
            return None
        d = json.loads(p.read_text())
        return Session(**d)

    def list(self) -> list[Session]:
        out = []
        for p in self.dir.glob("*.json"):
            try:
                out.append(Session(**json.loads(p.read_text())))
            except (ValueError, TypeError):
                continue
        return sorted(out, key=lambda s: s.updated, reverse=True)

    def latest(self) -> Session | None:
        items = self.list()
        return items[0] if items else None
