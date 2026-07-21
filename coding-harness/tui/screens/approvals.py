"""Global approval inbox for signal-based WAIT checkpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from ..api import ConductorError, PendingApproval
from ..widgets.factory_bar import FactoryTopBar
from ..widgets.modals import ApprovalModal


class ApprovalInbox(Screen):
    BINDINGS = [Binding("enter", "open", "open"), Binding("r", "refresh", "refresh"),
                Binding("escape", "back", "back")]

    def __init__(self, *, auto_open_single: bool = False):
        super().__init__()
        self._items: list[PendingApproval] = []
        self._auto_open_single = auto_open_single
        self._loaded = False

    def compose(self) -> ComposeResult:
        yield FactoryTopBar()
        yield Static("Approval Inbox — pending checkpoints across all executions", id="launcher_title")
        yield DataTable(id="approval_table", cursor_type="row", zebra_stripes=True)
        yield Static("enter decide · legacy HUMAN rows require workflow re-registration · esc back",
                     id="dash_hint")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#approval_table", DataTable)
        table.add_columns("", "workflow", "target", "phase", "age", "artifact", "execution")
        self.refresh_data()
        self.set_interval(5.0, self.refresh_data)

    @work(exclusive=True, group="approvals")
    async def refresh_data(self) -> None:
        try:
            self._items = await self.app.client.pending_approvals()
        except ConductorError:
            return
        self._loaded = True
        table = self.query_one("#approval_table", DataTable)
        table.clear()
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        for item in self._items:
            target = item.input.get("repo") or item.input.get("repoPath") or \
                (f"PR #{item.input.get('prNumber')}" if item.input.get("prNumber") else
                 f"issue #{item.input.get('issueNumber')}" if item.input.get("issueNumber") else "—")
            age_s = max(0, (now - (item.scheduled_ms or now)) // 1000)
            age = f"{age_s // 3600}h" if age_s >= 3600 else f"{age_s // 60}m"
            draft = item.draft
            artifact = draft.get("title") or draft.get("summary") or \
                (f"{len(draft.get('comments') or [])} comments" if draft else "—")
            table.add_row("⚠" if item.legacy else "●", item.workflow, str(target),
                          str(item.input.get("phase") or item.task_ref), age,
                          str(artifact)[:70], item.workflow_id[:8])
        self._maybe_auto_open_single()

    def request_auto_open_single(self) -> None:
        self._auto_open_single = True
        self._maybe_auto_open_single()

    def _maybe_auto_open_single(self) -> None:
        if not self._auto_open_single or not self._loaded:
            return
        actionable = [(index, item) for index, item in enumerate(self._items) if not item.legacy]
        self._auto_open_single = False
        if len(actionable) != 1:
            return
        index, _ = actionable[0]
        self.query_one("#approval_table", DataTable).move_cursor(row=index)
        self.call_after_refresh(self.action_open)

    def _selected(self) -> PendingApproval | None:
        table = self.query_one("#approval_table", DataTable)
        return self._items[table.cursor_row] if 0 <= table.cursor_row < len(self._items) else None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open()

    def action_open(self) -> None:
        item = self._selected()
        if not item:
            return
        if item.legacy:
            self.notify("Legacy HUMAN checkpoint: register current workflows and relaunch; WAIT signaling is incompatible.",
                        severity="warning", timeout=10)
            return

        async def decide(status: str, output: dict) -> None:
            try:
                await self.app.client.signal_task(item.workflow_id, item.task_ref, status, output,
                                                  task_type=item.task_type)
                await self.app.poll_approvals()
                self.refresh_data()
            except ConductorError as exc:
                self.notify(str(exc), severity="error")

        def on_decision(status: str, output: dict) -> None:
            self.run_worker(decide(status, output), exclusive=True, group="approval-decision")

        self.app.push_screen(ApprovalModal(
            item.workflow, item.draft,
            pr_number=item.input.get("prNumber"), issue_number=item.input.get("issueNumber"),
            workspace_path=item.input.get("repoPath"), on_decision=on_decision,
        ))

    def action_refresh(self) -> None:
        self.refresh_data()

    def action_back(self) -> None:
        self.app.pop_screen()
