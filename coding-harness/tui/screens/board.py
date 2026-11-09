"""Team board screen (§4.8) — watch a team_build / cc_program run's boards live and steer it.

The terminal twin of the web Swarm Console. It holds no logic of its own: the view is the console's
``board_json`` (fold + exception queue + constraints in force + child teams) and every action goes
through the console's ``apply_steer`` — the same authored board write, so the two surfaces can never
drift. The author is ``board.current_author()``: the registered actor for ``SWARM_ACTOR_TOKEN``
(``human:<email>``, with override authority), or plain ``human`` without one (ranked as an agent).

The message line is focused on open: type what you want in your own words and press enter — it is
routed to the right item, child team, or the whole team (``steer_message``). esc moves to the item
list, where single-key shortcuts act on the highlighted row (tab cycles items → constraints → teams).
"""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static

from .. import board as board_mod
from .. import console as console_mod
from ..widgets.factory_bar import FactoryTopBar

REFRESH_SECONDS = 4.0

# Actions that take free text from the input line; the rest act immediately.
_TEXT_ACTIONS = {
    "nudge": "advisory directive for {item} (enter to post)…",
    "stop": "blocking stop for {item} — reason (enter; blank = 'stop')…",
    "replace": "replacement constraint for {item} (enter)…",
}


_MESSAGE_PLACEHOLDER = "message the team in your own words — enter sends (esc: item shortcuts)"


class TeamBoard(Screen):
    DEFAULT_CSS = """
    #board_body { height: 1fr; }
    #board_left { width: 3fr; }
    #board_right { width: 2fr; }
    #board_items { height: 1fr; }
    #board_constraints, #board_children { height: auto; max-height: 8; }
    #board_exceptions { height: auto; max-height: 8; }
    #board_trace { height: 1fr; }
    """
    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("r", "refresh", "refresh"),
        Binding("n", "text('nudge')", "nudge"),
        Binding("s", "text('stop')", "stop"),
        Binding("o", "steer('reopen')", "reopen"),
        Binding("t", "terminate", "terminate run"),
        Binding("l", "lift", "lift constraint"),
        Binding("e", "text('replace')", "replace constraint"),
        Binding("m", "message", "message"),
    ]

    def __init__(self, correlation_id: str, blackboard_dir: str | None = None):
        super().__init__()
        self.correlation_id = correlation_id
        self.blackboard_dir = blackboard_dir
        self.author = board_mod.current_author()
        self._items: list[dict] = []
        self._constraints: list[dict] = []
        self._children: list[dict] = []
        self._mode: str | None = None
        self._mode_item = ""
        self._mode_directive = ""

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield FactoryTopBar()
        authority = "override authority" if self.author.startswith("human:") else \
            "[yellow]no SWARM_ACTOR_TOKEN — steering ranks as an agent[/yellow]"
        yield Static(f"Board — run {self.correlation_id} · as {self.author} ({authority})", id="launcher_title")
        yield Static("", id="board_summary")
        with Horizontal(id="board_body"):
            with Vertical(id="board_left"):
                yield Label("Items")
                yield ListView(id="board_items")
            with Vertical(id="board_right"):
                yield Label("Escalations (for awareness — the team decides)")
                yield Static("", id="board_exceptions")
                yield Label("Constraints in force")
                yield ListView(id="board_constraints")
                yield Label("Child teams (enter opens)")
                yield ListView(id="board_children")
                yield Label("Trace — highlighted item")
                yield Static("", id="board_trace")
        yield Input(placeholder=_MESSAGE_PLACEHOLDER, id="board_directive")
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()
        self.set_interval(REFRESH_SECONDS, self.action_refresh)
        self.query_one("#board_directive", Input).focus()

    # ------------------------------------------------------------------ read
    def action_refresh(self) -> None:
        try:
            view = console_mod.board_json(self.correlation_id, blackboard_dir=self.blackboard_dir)
        except Exception as exc:  # noqa: BLE001
            self.query_one("#board_summary", Static).update(f"[red]board read failed: {exc}[/red]")
            return
        r = view["rollup"]
        self.query_one("#board_summary", Static).update(
            f"{r['done']}/{r['total']} done ({int(r['progress'] * 100)}%)  ·  spend "
            f"${view['spend'].get('usd', 0):.2f}  ·  lessons {r['lessons']}  ·  open-reviews "
            f"{r['openReviews']}  ·  exceptions {len(view['exceptions'])}")
        owners = view["owners"]
        self._items = view["items"]
        self._fill("#board_items", [
            escape(f"[{it.get('state'):11}] {it.get('id'):14} {(it.get('title') or it.get('description') or '')[:40]}")
            + (f" [dim]holds {', '.join(f for f, o in owners.items() if o == it.get('id'))}[/dim]"
               if any(o == it.get("id") for o in owners.values()) else "")
            for it in self._items])
        self._constraints = view["constraints"]
        self._fill("#board_constraints", [
            escape(f"{d.get('id', '')[:8]} {d.get('authority', 'agent'):5} → {d.get('target')}: "
                   f"{(d.get('text') or '')[:50]}") + (" [red](blocking)[/red]" if d.get("urgency") == "blocking" else "")
            for d in self._constraints] or ["[dim](none)[/dim]"])
        self._children = view["children"]
        self._fill("#board_children", [
            escape(f"{c.get('subsystem')} [{c.get('kind')}] {c['rollup'].get('done', 0)}/"
                   f"{c['rollup'].get('total', 0)} {c.get('verdict') or 'running'}") + (f" [red]{c['openExceptions']} exc[/red]" if c.get("openExceptions") else "")
            for c in self._children] or ["[dim](none)[/dim]"])
        self.query_one("#board_exceptions", Static).update("\n".join(
            f"[red]{escape(str(e.get('kind')))}[/red] {escape(str(e.get('item')))}: "
            f"{escape(str(e.get('detail') or '')[:70])}"
            for e in view["exceptions"]) or "[dim](none)[/dim]")
        self._show_trace()

    def _fill(self, selector: str, rows: list[str]) -> None:
        lv = self.query_one(selector, ListView)
        idx = lv.index
        lv.clear()
        for row in rows:
            lv.append(ListItem(Label(row)))
        if rows:
            lv.index = min(idx or 0, len(rows) - 1)

    def _show_trace(self) -> None:
        iid = self._selected(self._items, "#board_items").get("id")
        if not iid:
            self.query_one("#board_trace", Static).update("[dim](no item)[/dim]")
            return
        from .. import board_trace
        events = board_trace.item_timeline(self.correlation_id, iid)[-10:]
        self.query_one("#board_trace", Static).update("\n".join(
            escape(f"{e['kind']:10} {str(e.get('author') or '')[:22]:22} {e['summary'][:60]}") for e in events)
            or "[dim](no facts)[/dim]")

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "board_items":
            self._show_trace()

    def _selected(self, rows: list[dict], selector: str) -> dict:
        lv = self.query_one(selector, ListView)
        return rows[lv.index] if lv.index is not None and lv.index < len(rows) else {}

    # ------------------------------------------------------------------ write
    def _steer(self, action: str, item: str = "", text: str = "", directive: str = "") -> None:
        res = console_mod.apply_steer(
            {"run": self.correlation_id, "action": action, "item": item, "text": text, "directive": directive},
            blackboard_dir=self.blackboard_dir, author=self.author)
        if res.get("ok"):
            self.notify(f"{action} {item or directive} — recorded as {self.author}", timeout=4)
        else:
            self.notify(f"{action} failed: {res.get('error')}", severity="error", timeout=6)
        self.action_refresh()

    def action_terminate(self) -> None:
        """The human's hard stop: terminate this run and its agents (press t twice within 3s)."""
        import time
        now = time.monotonic()
        if now - getattr(self, "_terminate_armed", 0.0) > 3.0:
            self._terminate_armed = now
            self.notify("press t again to terminate this run", severity="warning", timeout=3)
            return
        self._terminate_armed = 0.0
        self._steer("terminate", text="from TUI board")

    def action_steer(self, action: str) -> None:
        """Immediate actions on the highlighted item."""
        item = self._selected(self._items, "#board_items").get("id") or ""
        if not item:
            self.notify("select an item first", severity="warning")
            return
        self._steer(action, item=item)

    def action_lift(self) -> None:
        d = self._selected(self._constraints, "#board_constraints")
        if not d.get("id"):
            self.notify("select a constraint (tab to the constraints list)", severity="warning")
            return
        self._steer("lift", item=str(d.get("target") or ""), text="lifted from TUI", directive=str(d["id"]))

    def action_text(self, action: str) -> None:
        if action == "replace":
            d = self._selected(self._constraints, "#board_constraints")
            if not d.get("id"):
                self.notify("select a constraint (tab to the constraints list)", severity="warning")
                return
            self._mode_item, self._mode_directive = str(d.get("target") or ""), str(d["id"])
        else:
            self._mode_item = self._selected(self._items, "#board_items").get("id") or ""
            self._mode_directive = ""
            if not self._mode_item:
                self.notify("select an item first", severity="warning")
                return
        self._mode = action
        box = self.query_one("#board_directive", Input)
        box.placeholder = _TEXT_ACTIONS[action].format(item=self._mode_item)
        box.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        mode, item, directive = self._mode, self._mode_item, self._mode_directive
        self._mode, self._mode_item, self._mode_directive = None, "", ""
        event.input.value = ""
        event.input.placeholder = _MESSAGE_PLACEHOLDER
        if mode is None:
            # The default: a plain-language message, routed to the right item/team/action for you.
            if text:
                self.notify("routing your message…", timeout=3)
                self.run_worker(lambda: self._send_message(text), thread=True, exclusive=False)
            return
        if mode in ("nudge", "replace") and not text:
            return
        self._steer(mode, item=item, text=text, directive=directive)
        self.query_one("#board_items", ListView).focus()

    def _send_message(self, text: str) -> None:
        from .. import steer_message
        try:
            res = steer_message.send(self.correlation_id, text, author=self.author, blackboard_dir=self.blackboard_dir)
            line, ok = steer_message.describe(res), res.get("ok")
        except Exception as exc:  # noqa: BLE001
            line, ok = f"message not sent: {exc}", False
        self.app.call_from_thread(self._report, line, bool(ok))

    def _report(self, line: str, ok: bool) -> None:
        self.notify(line, severity="information" if ok else "error", timeout=10)
        self.action_refresh()

    def action_message(self) -> None:
        self.query_one("#board_directive", Input).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a child team drills into that team's own board (H3)."""
        if event.list_view.id == "board_children":
            child = self._selected(self._children, "#board_children").get("run")
            if child:
                self.app.push_screen(TeamBoard(child, self.blackboard_dir))

    def action_back(self) -> None:
        """esc in the message line hands the keyboard to the item list (where the shortcuts act);
        esc anywhere else leaves the board."""
        box = self.query_one("#board_directive", Input)
        if self.focused is box:
            self._mode, self._mode_item, self._mode_directive = None, "", ""
            box.placeholder = _MESSAGE_PLACEHOLDER
            self.query_one("#board_items", ListView).focus()
            return
        self.app.pop_screen()
