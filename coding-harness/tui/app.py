"""The Textual application shell: owns the Conductor client, chat LLM client, settings,
and the current chat session; lands on Chat by default (or Dashboard with --dashboard)."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timezone

from textual.app import App

from .api import ConductorClient
from .chat.session import Session, SessionStore
from .config import Settings
from .model_profiles import bootstrap_profiles
from .worker_supervisor import WorkerSupervisor


class HarnessApp(App):
    CSS_PATH = "theme.tcss"
    TITLE = "Conductor Software Factory"

    def __init__(self, settings: Settings, client=None, resume: str | None = None,
                 start_dashboard: bool = False, manage_workers: bool = True,
                 worker_supervisor=None):
        super().__init__()
        self.settings = settings
        self.model_profile_starter = bootstrap_profiles()
        self.client = client or ConductorClient(settings.server_url)
        self.worker_supervisor = worker_supervisor
        if self.worker_supervisor is None and client is None and manage_workers:
            self.worker_supervisor = WorkerSupervisor(settings.server_url)
        self.session_runs: set[str] = set()   # runs started/opened this session → notify
        self.notified: set[str] = set()
        self.pending_approvals = []
        self.approval_ids: set[str] = set()
        self._approval_poll_started = False
        self._approval_signal_registered = False
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

    async def on_mount(self) -> None:
        self._install_approval_signal()
        if self.worker_supervisor is not None:
            started = await self.worker_supervisor.start()
            if not started:
                reason = self.worker_supervisor.last_error or "unknown error"
                self.notify(f"workers did not start: {reason}", severity="warning", timeout=12)
        if self._start_dashboard:
            from .screens.dashboard import Dashboard
            self.push_screen(Dashboard())
        else:
            from .screens.chat import Chat
            self.push_screen(Chat())
        self.set_interval(5.0, self.poll_approvals)
        await self.poll_approvals()

    async def on_unmount(self) -> None:
        if self._approval_signal_registered:
            asyncio.get_running_loop().remove_signal_handler(signal.SIGUSR1)
            self._approval_signal_registered = False
        if self.worker_supervisor is not None:
            await self.worker_supervisor.stop()
        await self.client.aclose()

    def track(self, workflow_id: str) -> None:
        if workflow_id:
            self.session_runs.add(workflow_id)

    def _install_approval_signal(self) -> None:
        """Route notification clicks back into this running TUI process."""
        try:
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGUSR1,
                lambda: self.call_later(self.open_approvals_from_notification),
            )
            self._approval_signal_registered = True
        except (AttributeError, NotImplementedError, RuntimeError, ValueError):
            self._approval_signal_registered = False

    def open_approvals_from_notification(self) -> None:
        from .screens.approvals import ApprovalInbox
        from .widgets.modals import ApprovalModal

        if isinstance(self.screen, ApprovalModal):
            return
        if isinstance(self.screen, ApprovalInbox):
            self.screen.request_auto_open_single()
            return
        self.push_screen(ApprovalInbox(auto_open_single=True))

    async def poll_approvals(self) -> None:
        """Global inbox poller; runs regardless of the visible screen."""
        from . import notify
        from .api import ConductorError
        method = getattr(self.client, "pending_approvals", None)
        if method is None:  # lightweight test/embedding clients may expose only run APIs
            return
        try:
            pending = await method()
        except ConductorError:
            return
        current = {item.task_id for item in pending}
        new = [item for item in pending if item.task_id not in self.approval_ids]
        if not self._approval_poll_started:
            if pending:
                notify.notify(
                    self.settings.notify,
                    "Conductor approvals",
                    f"{len(pending)} approval{'s' if len(pending) != 1 else ''} waiting",
                    open_approvals=True,
                )
            self._approval_poll_started = True
        else:
            for item in new:
                target = item.input.get("repo") or item.input.get("repoPath") or ""
                phase = item.input.get("phase") or item.task_ref
                notify.notify(
                    self.settings.notify,
                    "Approval requested",
                    f"{item.workflow} · {target} · {phase}",
                    self.settings.execution_url(item.workflow_id),
                    open_approvals=True,
                )
        self.pending_approvals = pending
        self.approval_ids = current
