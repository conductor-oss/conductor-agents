"""Templates manager — browse, edit, create, and delete the local prompt-template library.

Templates live as markdown files under ``~/.conductor-harness/templates/`` (see
``tui/templates.py``). Editing opens the file in your external editor (the same bridge as
`e` elsewhere) — the TUI doesn't embed an editor. Reachable from the Dashboard (`t`) and
chat (`/templates`).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Label, ListItem, ListView, Static

from .. import catalog, edit, templates
from ..widgets.factory_bar import FactoryTopBar
from ..widgets.modals import ConfirmModal, NewTemplateModal


class TemplatesScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("e", "edit", "edit"),          # enter also edits (via ListView.Selected)
        Binding("n", "new", "new"),
        Binding("d", "delete", "delete"),
        Binding("r", "reload", "reload"),
    ]

    def compose(self) -> ComposeResult:
        yield FactoryTopBar()
        yield Static("Prompt templates — enter/e edit in editor · n new · d delete · esc back",
                     id="launcher_title")
        yield ListView(id="tpl_list")
        yield Static("", id="tpl_hint", classes="muted")
        yield Footer()

    def on_mount(self) -> None:
        self._reload()

    # ------------------------------------------------------------------ data
    def _reload(self) -> None:
        lv = self.query_one("#tpl_list", ListView)
        lv.clear()
        self._entries = templates.list_templates()
        for e in self._entries:
            scope = ", ".join(e.workflows) if e.workflows else "all workflows"
            if e.repos:
                scope += " · repos: " + ", ".join(e.repos)
            desc = f"\n  [dim]{e.description}[/dim]" if e.description else ""
            item = ListItem(Label(f"{e.name}  [dim]· {scope}[/dim]{desc}"))
            item.data = e
            lv.append(item)
        if self._entries:
            lv.index = 0
        self.query_one("#tpl_hint", Static).update(
            f"{len(self._entries)} template(s) in {templates.templates_dir()}"
            if self._entries else
            f"no templates yet — press n to create one in {templates.templates_dir()}")
        lv.focus()

    def _selected(self):
        lv = self.query_one("#tpl_list", ListView)
        idx = lv.index
        if self._entries and idx is not None and 0 <= idx < len(self._entries):
            return self._entries[idx]
        return None

    # enter on a row → edit it
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        entry = getattr(event.item, "data", None)
        if entry is not None:
            self._open(entry)

    # ------------------------------------------------------------------ actions
    def action_edit(self) -> None:
        entry = self._selected()
        if entry is None:
            self.app.bell()
            return
        self._open(entry)

    def _open(self, entry) -> None:
        self.notify(edit.open_path(self.app, str(entry.path), self.app.settings.editor))

    def action_new(self) -> None:
        self.app.push_screen(NewTemplateModal(list(catalog.LAUNCHABLE), on_create=self._create))

    def _create(self, name: str, workflows: tuple[str, ...], repos: tuple[str, ...] = ()) -> None:
        # seed from the shipped default prompt for the scoped workflow (if any)
        key = templates.WORKFLOW_KEY.get(workflows[0]) if workflows else None
        entry = templates.create(name, key=key, workflows=workflows, repos=repos)
        self._reload()
        self._open(entry)   # jump straight into editing the new file (pre-filled with the default)

    def action_delete(self) -> None:
        entry = self._selected()
        if entry is None:
            self.app.bell()
            return
        self.app.push_screen(ConfirmModal(
            "Delete template", f"Delete '{entry.name}' ({entry.path.name})? This cannot be undone.",
            confirm_label="Delete", on_confirm=lambda _reason: self._do_delete(entry)))

    def _do_delete(self, entry) -> None:
        templates.delete(entry)
        self.notify(f"deleted {entry.path.name}")
        self._reload()

    def action_reload(self) -> None:
        self._reload()

    def action_back(self) -> None:
        self.app.pop_screen()

    # refresh when returning from a terminal-editor suspend or a pushed modal
    def on_screen_resume(self) -> None:
        self._reload()
