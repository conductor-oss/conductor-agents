"""The Textual application shell: owns the Conductor client, chat LLM client, settings,
and the current chat session; lands on Chat by default (or Dashboard with --dashboard)."""

from __future__ import annotations

import os

from textual.app import App

from .api import ConductorClient
from .chat.session import Session, SessionStore
from .config import Settings


class HarnessApp(App):
    CSS_PATH = "theme.tcss"
    TITLE = "Conductor Coding Harness"

    def __init__(self, settings: Settings, client=None, resume: str | None = None,
                 start_dashboard: bool = False):
        super().__init__()
        self.settings = settings
        self.client = client or ConductorClient(settings.server_url)
        self.session_runs: set[str] = set()   # runs started/opened this session → notify
        self.notified: set[str] = set()
        self._start_dashboard = start_dashboard

        # Chat LLM client (Anthropic) — only if a key is present; else chat shows guidance.
        self.llm_client = self._make_llm_client()

        # Current chat session (resumed or new).
        self.session_store = SessionStore()
        self.session = self._resolve_session(resume)

    def _make_llm_client(self):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        try:
            from anthropic import AsyncAnthropic
            return AsyncAnthropic()
        except Exception:  # noqa: BLE001
            return None

    def _resolve_session(self, resume: str | None) -> Session:
        if resume == "last":
            s = self.session_store.latest()
            if s:
                return s
        elif resume:
            s = self.session_store.load(resume)
            if s:
                return s
        return Session.new(self.settings.model)

    def on_mount(self) -> None:
        if self._start_dashboard:
            from .screens.dashboard import Dashboard
            self.push_screen(Dashboard())
        else:
            from .screens.chat import Chat
            self.push_screen(Chat())

    async def on_unmount(self) -> None:
        await self.client.aclose()

    def track(self, workflow_id: str) -> None:
        if workflow_id:
            self.session_runs.add(workflow_id)
