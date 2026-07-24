"""Global factory identity and at-a-glance operational statistics."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from ..api import ConductorError, Run


_BANNER = r"""  _________  _  _____  __  _________________  ___
 / ___/ __ \/ |/ / _ \/ / / / ___/_  __/ __ \/ _ \
/ /__/ /_/ /    / // / /_/ / /__  / / / /_/ / , _/
\___/\____/_/|_/____/\____/\___/ /_/  \____/_/|_|"""


class FactoryTopBar(Vertical):
    """Compact title bar shared by every primary TUI screen."""

    def compose(self) -> ComposeResult:
        yield Static(self._logo(), id="factory_logo")
        with Horizontal(id="factory_meta"):
            yield Static(
                "Conductor Software Factory  //  build · orchestrate · verify",
                id="factory_title",
            )
            yield Static("connecting…", id="factory_stats")

    def on_mount(self) -> None:
        self.refresh_stats()
        self.set_interval(5.0, self.refresh_stats)

    @staticmethod
    def _logo() -> Text:
        return Text(_BANNER, style="bold #f2f3f5", no_wrap=True, overflow="crop")

    @staticmethod
    def _server_label(url: str) -> str:
        parsed = urlparse(url)
        return parsed.netloc or parsed.path.rstrip("/") or "Conductor"

    @staticmethod
    def _counts(runs: list[Run]) -> tuple[int, int]:
        active = sum(1 for run in runs if run.running)
        failed = sum(
            1 for run in runs
            if run.status.startswith("FAIL") or run.status == "TIMED_OUT"
        )
        return active, failed

    @work(exclusive=True, group="factory-stats")
    async def refresh_stats(self) -> None:
        """Refresh recent-run and worker summaries without blocking the UI."""
        try:
            runs, states = await asyncio.gather(
                self.app.client.search_runs(limit=50),
                self.app.client.health(),
            )
        except ConductorError:
            self._render_offline()
            return

        active, failed = self._counts(runs)
        worker_total = len(states)
        worker_alive = sum(1 for state in states.values() if state.alive)
        degraded = worker_alive < worker_total

        stats = Text(justify="right", no_wrap=True, overflow="crop")
        stats.append(self._server_label(self.app.settings.server_url), style="grey70")
        stats.append("  ● ONLINE", style="bold green")
        stats.append(
            f" · recent {len(runs)} · active {active} · failed {failed} · "
            f"workers {worker_alive}/{worker_total}",
            style="bold white" if degraded else "grey70",
        )
        approvals = len(getattr(self.app, "pending_approvals", []))
        if approvals:
            stats.append(f" · approvals {approvals}", style="bold yellow")
        self.query_one("#factory_stats", Static).update(stats)
        self.remove_class("-down")
        self.set_class(degraded, "-degraded")

    def _render_offline(self) -> None:
        stats = Text(justify="right", no_wrap=True, overflow="crop")
        stats.append(self._server_label(self.app.settings.server_url), style="grey70")
        stats.append("  ● OFFLINE", style="bold red")
        stats.append(" · server unreachable · stats unavailable", style="bold red")
        self.query_one("#factory_stats", Static).update(stats)
        self.remove_class("-degraded")
        self.add_class("-down")
