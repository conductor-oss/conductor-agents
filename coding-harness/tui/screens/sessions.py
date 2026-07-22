"""Sessions picker — browse and resume past chat sessions."""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Label, ListItem, ListView, Static

from ..widgets.factory_bar import FactoryTopBar


def _ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


class Sessions(Screen):
    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("n", "new", "new session"),
    ]

    def compose(self) -> ComposeResult:
        yield FactoryTopBar()
        yield Static("Sessions — enter to resume · n new · esc back", id="launcher_title")
        yield ListView(id="session_list")
        yield Footer()

    def on_mount(self) -> None:
        lv = self.query_one("#session_list", ListView)
        self._sessions = self.app.session_store.list()
        if not self._sessions:
            lv.append(ListItem(Label("(no saved sessions yet)")))
        for s in self._sessions:
            runs = f" · {len(s.runs)} run(s)" if s.runs else ""
            item = ListItem(Label(f"{s.title}\n  [dim]{_ago(s.updated)} · {s.model}{runs}[/dim]"))
            item.data = s.id
            lv.append(item)
        if len(lv):
            lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        sid = getattr(event.item, "data", None)
        if not sid:
            return
        s = self.app.session_store.load(sid)
        if s:
            self.app.session = s
        self.app.pop_screen()
        scr = self.app.screen
        if hasattr(scr, "reload_session"):
            scr.reload_session()

    def action_new(self) -> None:
        from ..chat.session import Session
        self.app.session = Session.new(self.app.settings.model)
        self.app.pop_screen()
        scr = self.app.screen
        if hasattr(scr, "reload_session"):
            scr.reload_session()

    def action_back(self) -> None:
        self.app.pop_screen()
