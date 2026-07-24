"""Dashboard (home) — the fleet: recent runs, live tokens/cost, worker health."""

from __future__ import annotations

import webbrowser

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Static

from .. import format as fmt, notify
from ..api import ConductorError, Run, TERMINAL
from ..widgets.factory_bar import FactoryTopBar

_FILTERS = ["ALL", "RUNNING", "FAILED"]
_RUNNING = {"RUNNING", "SCHEDULED", "IN_PROGRESS", "PAUSED"}


class Dashboard(Screen):
    BINDINGS = [
        Binding("n", "new_run", "new"),
        Binding("enter", "open_run", "open", priority=True),
        Binding("f", "cycle_filter", "filter"),
        Binding("slash", "search", "search"),
        Binding("escape", "close_search", "", show=False),
        Binding("o", "open_browser", "browser"),
        Binding("e", "open_folder", "editor"),
        Binding("t", "templates", "templates"),
        Binding("m", "model_profiles", "models"),
        Binding("a", "approvals", "approvals"),
        Binding("s", "automations", "automations"),
        Binding("g", "register_workflows", "register"),
        Binding("r", "refresh_now", "refresh"),
        Binding("q", "quit_app", "quit"),
        Binding("question_mark", "help", "help"),
    ]

    def __init__(self):
        super().__init__()
        self._runs: list[Run] = []       # all fetched, newest first
        self._shown: list[Run] = []      # after filter/search, aligned to table rows
        self._filter = 0                 # index into _FILTERS
        self._search = ""

    def compose(self) -> ComposeResult:
        yield FactoryTopBar()
        with Vertical():
            self._search_input = Input(placeholder="filter: target / id / workflow …", id="filter")
            self._search_input.display = False
            yield self._search_input
            table = DataTable(id="run_table", cursor_type="row", zebra_stripes=True)
            yield table
            yield Static(self._hint(), id="dash_hint")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#run_table", DataTable)
        table.add_columns("", "workflow", "id", "target", "dur", "tokens", "cost")
        table.focus()  # keep focus off the hidden search Input so key bindings fire
        self.refresh_data()
        self.set_interval(5.0, self.refresh_data)

    # ------------------------------------------------------------------ data
    @work(exclusive=True, group="dash")
    async def refresh_data(self) -> None:
        try:
            self._runs = await self.app.client.search_runs(limit=50)
        except ConductorError:
            return
        self._check_notifications()
        self._repopulate()

    def _check_notifications(self) -> None:
        """Notify once when a run started/opened this session reaches a terminal state."""
        for r in self._runs:
            if (r.id in self.app.session_runs and r.status in TERMINAL
                    and r.id not in self.app.notified):
                self.app.notified.add(r.id)
                tt, tc = self._live_tokens_cost(r)
                notify.notify(self.app.settings.notify,
                              f"{r.workflow} — {r.status}",
                              f"{r.target} · {fmt.cost(tc)}",
                              url=self.app.settings.execution_url(r.id))

    def _repopulate(self) -> None:
        table = self.query_one("#run_table", DataTable)
        prev = self._shown[table.cursor_row].id if self._shown and table.cursor_row < len(self._shown) else None
        self._shown = [r for r in self._runs if self._matches(r)]
        table.clear()
        for r in self._shown:
            glyph = Text(f"{fmt.status_glyph(r.status)} {fmt.status_label(r.status)}",
                         style=fmt.status_color(r.status))
            tt, tc = self._live_tokens_cost(r)
            table.add_row(
                glyph, r.workflow, fmt.short_id(r.id), r.target,
                fmt.duration(r.duration_ms()), fmt.tokens(tt), fmt.cost(tc),
                key=r.id,
            )
        # restore selection
        if prev:
            for i, r in enumerate(self._shown):
                if r.id == prev:
                    table.move_cursor(row=i)
                    break
        self.query_one("#dash_hint", Static).update(self._hint())

    def _live_tokens_cost(self, r: Run):
        """Raw token/cost values (may be int, None, or the literal 'null' string from
        search's Java-map serialization) — the fmt helpers coerce to a display string."""
        o = r.output or {}
        tt = o.get("totalTokens") if o.get("totalTokens") is not None else o.get("tokenUsed")
        tc = o.get("totalCostUsd") if o.get("totalCostUsd") is not None else o.get("costUsd")
        return tt, tc

    def _matches(self, r: Run) -> bool:
        f = _FILTERS[self._filter]
        if f == "RUNNING" and r.status not in _RUNNING:
            return False
        if f == "FAILED" and not r.status.startswith("FAIL") and r.status != "TIMED_OUT":
            return False
        if self._search:
            hay = f"{r.workflow} {r.id} {r.target}".lower()
            if self._search.lower() not in hay:
                return False
        return True

    def _hint(self) -> str:
        n = len(self._shown)
        return f"{n} run(s) · filter: {_FILTERS[self._filter]}" + (
            f" · search: {self._search!r}" if self._search else "") + " · newest first, refresh 5s"

    def _selected(self) -> Run | None:
        table = self.query_one("#run_table", DataTable)
        if self._shown and 0 <= table.cursor_row < len(self._shown):
            return self._shown[table.cursor_row]
        return None

    # ------------------------------------------------------------------ actions
    def action_refresh_now(self) -> None:
        self.refresh_data()

    def action_model_profiles(self) -> None:
        from .model_profiles import ModelProfiles
        self.app.push_screen(ModelProfiles())

    def action_cycle_filter(self) -> None:
        self._filter = (self._filter + 1) % len(_FILTERS)
        self._repopulate()

    def action_quit_app(self) -> None:
        self.app.exit()

    def action_search(self) -> None:
        inp = self._search_input
        inp.display = True
        inp.focus()

    def action_close_search(self) -> None:
        """Escape: close the search box if open; otherwise go back (when not the base
        screen, e.g. opened from chat via /dashboard)."""
        inp = self._search_input
        if inp.display:
            inp.value = ""
            inp.display = False
            self._search = ""
            self._repopulate()
            self.query_one("#run_table", DataTable).focus()
        elif len(self.app.screen_stack) > 1:
            self.app.pop_screen()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._search = event.value
            self._repopulate()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            if not event.value:
                event.input.display = False
            self.query_one("#run_table", DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open_run()

    def action_open_run(self) -> None:
        run = self._selected()
        if not run:
            return
        from .run_detail import RunDetail
        self.app.track(run.id)
        self.app.push_screen(RunDetail(run.id))

    def action_new_run(self) -> None:
        # Always start fresh from the action picker. (Re-run-with-prefill lives in Run
        # Detail, where the target run is unambiguous — `n` there.)
        from .launcher import Launcher
        self.app.push_screen(Launcher())

    def action_templates(self) -> None:
        from .templates import TemplatesScreen
        self.app.push_screen(TemplatesScreen())

    def action_approvals(self) -> None:
        from .approvals import ApprovalInbox
        self.app.push_screen(ApprovalInbox())

    def action_automations(self) -> None:
        from .automations import AutomationsScreen
        self.app.push_screen(AutomationsScreen())

    @work(exclusive=True, group="registration")
    async def action_register_workflows(self) -> None:
        from ..registration import register_definitions
        from ..widgets.modals import ConfirmModal
        confirmed = await self.app.push_screen_wait(ConfirmModal(
            "Update workflow definitions",
            f"Register all harness task and workflow definitions on\n"
            f"{self.app.settings.server_url}?",
            confirm_label="Register",
        ))
        if not confirmed:
            return
        self.notify("registering task and workflow definitions…", timeout=10)
        result = await register_definitions(self.app.settings.server_url)
        if result.ok:
            self.notify("workflow definitions updated; worker gate passed", timeout=8)
            self.refresh_data()
        else:
            tail = result.output[-500:] if result.output else "unknown registration error"
            self.notify(f"registration failed: {tail}", severity="error", timeout=12)

    def action_open_browser(self) -> None:
        run = self._selected()
        if run:
            webbrowser.open(self.app.settings.execution_url(run.id))

    @work(group="openfolder")
    async def action_open_folder(self) -> None:
        import os
        from .. import api, edit
        run = self._selected()
        if not run:
            return
        path = api.workspace_path(run)
        if not path:  # not in search input/output → fetch the execution for the git_clone task
            try:
                detail = await self.app.client.get_run(run.id, recurse=False)
                path = detail.workspace()
            except ConductorError:
                path = None
        if not path or not os.path.isdir(path):
            self.notify("working dir not on this host (remote workers, or a cleaned /tmp clone)",
                        severity="warning")
            return
        self.notify(edit.open_path(self.app, path, self.app.settings.editor))

    def action_help(self) -> None:
        self.app.push_screen(HelpModal())


class HelpModal(Screen):
    BINDINGS = [Binding("escape,q,question_mark", "dismiss", "close")]

    def compose(self) -> ComposeResult:
        from textual.containers import Center, Middle
        keys = [
            ("n", "new run (prefilled from selection)"),
            ("enter", "open run detail"),
            ("f", "cycle filter (ALL/RUNNING/FAILED)"),
            ("/", "search"),
            ("o", "open in browser (Conductor UI)"),
            ("e", "open working folder in editor"),
            ("t", "manage prompt templates"),
            ("a", "approval inbox"),
            ("s", "manage automations"),
            ("g", "register/update workflow definitions"),
            ("r", "refresh now"),
            ("t", "terminate (run detail)"),
            ("l", "logs (run detail)"),
            ("y", "copy id (run detail)"),
            ("esc", "back"),
            ("q", "quit"),
        ]
        body = Text()
        for k, v in keys:
            body.append(f"  {k:<7}", style="bold cyan")
            body.append(f"{v}\n")
        with Middle():
            with Center():
                yield Static(body, id="help_box")

    def action_dismiss(self) -> None:
        self.app.pop_screen()
