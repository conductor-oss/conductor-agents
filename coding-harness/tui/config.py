"""Runtime settings for the TUI.

Inherits the same environment the `conductor` CLI uses (``CONDUCTOR_SERVER_URL``),
so no separate auth/config: point the TUI at whatever server the workers poll.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_SERVER = "http://localhost:8080/api"

# Friendly aliases for the chat driver model → the repo's current ids.
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
    "haiku": "claude-haiku-4-5",
}
DEFAULT_MODEL = "sonnet"


def resolve_model(name: str | None) -> str:
    m = (name or DEFAULT_MODEL).strip()
    return MODEL_ALIASES.get(m.lower(), m)


@dataclass(frozen=True)
class Settings:
    server_url: str          # Conductor API base, e.g. http://localhost:8080/api
    notify: bool = True      # OS notification + terminal bell on terminal states
    model: str = "claude-sonnet-4-6"   # chat driver model (resolved)
    editor: str | None = None          # override for opening working folders

    @property
    def web_base(self) -> str:
        """Web-UI base for `o`/`c` deep links: strip a trailing ``/api``.
        `http://localhost:8080/api` -> `http://localhost:8080`."""
        u = self.server_url.rstrip("/")
        return u[:-4] if u.endswith("/api") else u

    def execution_url(self, workflow_id: str) -> str:
        return f"{self.web_base}/execution/{workflow_id}"


def load(server: str | None = None, notify: bool = True, model: str | None = None,
         editor: str | None = None) -> Settings:
    """Resolve settings: explicit --server flag > CONDUCTOR_SERVER_URL > default."""
    url = server or os.environ.get("CONDUCTOR_SERVER_URL") or DEFAULT_SERVER
    ed = editor or os.environ.get("CONDUCTOR_HARNESS_EDITOR")
    return Settings(server_url=url.rstrip("/"), notify=notify,
                    model=resolve_model(model), editor=ed)
