"""Compact profile browser; policies are edited as validated JSON files."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from .. import edit
from ..model_profiles import ProfileError, bootstrap_profiles, load_profiles


class ModelProfiles(Screen):
    BINDINGS = [Binding("escape", "back", "back"), Binding("r", "reload", "reload"), Binding("e", "edit", "edit")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Model Profiles — workflow policies only; chat model is separate", id="models_title")
            yield DataTable(id="models_table")
            yield Static("e edit the single models.json · r reload · Esc back", id="models_hint")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#models_table", DataTable)
        table.add_columns("name", "default variant", "workflows", "repos", "sha")
        self.action_reload()

    def action_reload(self) -> None:
        table = self.query_one("#models_table", DataTable)
        table.clear()
        try:
            bootstrap_profiles()
            self._entries = load_profiles()
            for profile in self._entries:
                data = profile.data
                table.add_row(profile.name, str(data.get("defaultProfile", "standard")),
                              ", ".join(data.get("workflows", []) or []), ", ".join(data.get("repos", []) or []), profile.sha256[:12])
        except ProfileError as exc:
            self._entries = []
            self.query_one("#models_hint", Static).update(str(exc))

    def _selected(self):
        table = self.query_one("#models_table", DataTable)
        row = table.cursor_row
        return self._entries[row] if getattr(self, "_entries", []) and 0 <= row < len(self._entries) else None

    def action_edit(self) -> None:
        entry = self._selected()
        if entry is None:
            self.app.bell(); return
        self.notify(edit.open_path(self.app, str(entry.path), self.app.settings.editor))


    def action_back(self) -> None:
        self.app.pop_screen()
