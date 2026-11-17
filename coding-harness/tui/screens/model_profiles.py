"""The model policy every run uses: which agent and model each role (and each rescue tier) runs on.

Enter edits the highlighted role in place, d makes its profile the default, j edits the whole policy
as JSON in-app, e opens the file in your editor. The same file and validation as the web console's
Settings page; the chat model is separate."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from .. import edit
from ..model_profiles import ProfileError, role_rows, save_user_policy, set_role, user_policy


def _backends() -> list[str]:
    try:
        from common.model_policy import BACKENDS
        return sorted(BACKENDS)
    except Exception:  # noqa: BLE001
        return ["claude", "codex", "gemini"]


class ModelProfiles(Screen):
    BINDINGS = [Binding("escape", "back", "back"), Binding("enter", "edit_role", "edit role"),
                Binding("d", "make_default", "make default"), Binding("j", "json", "edit JSON"),
                Binding("e", "edit", "open in editor"), Binding("r", "reload", "reload")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Model Profiles: the agent and model each role of a run uses", id="models_title")
            yield DataTable(id="models_table", cursor_type="row")
            yield Static("enter edit role · d make default · j edit JSON · e open in editor · r reload · esc back",
                         id="models_hint")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#models_table", DataTable)
        table.add_columns("profile", "role", "tier", "agent", "model")
        self.action_reload()

    def action_reload(self) -> None:
        table = self.query_one("#models_table", DataTable)
        table.clear()
        self._policy = user_policy()
        data = self._policy.get("data") or {}
        self._rows = role_rows(data)
        default = str(data.get("defaultProfile") or "")
        for r in self._rows:
            tier = "" if r["tier"] is None else ("first try" if r["tier"] == 0 else f"rescue {r['tier']}")
            table.add_row(r["profile"] + ("  (default)" if r["profile"] == default else ""),
                          r["role"], tier, r["agent"], r["model"])
        problems = self._policy.get("problems") or []
        self.query_one("#models_title", Static).update(
            f"Model Profiles: {self._policy['path']}" + (f"  [red]{'; '.join(problems)}[/red]" if problems else ""))

    def _selected(self) -> dict | None:
        row = self.query_one("#models_table", DataTable).cursor_row
        rows = getattr(self, "_rows", [])
        return rows[row] if rows and 0 <= row < len(rows) else None

    def _save(self, data: dict) -> str | None:
        try:
            save_user_policy(data, expected_sha256=self._policy.get("sha256") or "")
        except ProfileError as exc:
            return str(exc)
        self.action_reload()
        return None

    def action_edit_role(self) -> None:
        row = self._selected()
        if row is None or not self._policy.get("data"):
            self.app.bell()
            return
        from .settings_edit import RoleModal

        def save(agent: str, model: str) -> str | None:
            err = self._save(set_role(self._policy["data"], row["profile"], row["role"], row["tier"],
                                      agent=agent, model=model))
            if not err:
                self.notify(f"{row['profile']} {row['role']} now runs {agent} {model}")
            return err
        self.app.push_screen(RoleModal(row, _backends(), on_save=save))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_edit_role()

    def action_make_default(self) -> None:
        row = self._selected()
        if row is None:
            self.app.bell()
            return
        err = self._save({**self._policy["data"], "defaultProfile": row["profile"]})
        self.notify(err or f"New runs use {row['profile']}", severity="error" if err else "information")

    def action_json(self) -> None:
        from .settings_edit import PolicyJsonEditor
        self.app.push_screen(PolicyJsonEditor(on_saved=self.action_reload))

    def action_edit(self) -> None:
        self.notify(edit.open_path(self.app, str(self._policy["path"]), self.app.settings.editor))

    def on_screen_resume(self) -> None:
        self.action_reload()

    def action_back(self) -> None:
        self.app.pop_screen()
