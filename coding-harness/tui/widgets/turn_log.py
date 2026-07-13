"""Live agent turn/command trace for a selected coding_agent task."""

from __future__ import annotations

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from .. import format as fmt
from ..api import AgentSnapshot, TaskNode


class TurnLog(VerticalScroll):
    def __init__(self):
        super().__init__(id="turn_log")
        self._body = Static("")

    def compose(self):
        yield self._body

    def show(self, task: TaskNode | None) -> None:
        if task is None:
            self._body.update(Text("Select a task to see its activity.", style="grey62"))
            return
        snap = task.snapshot()
        if snap is None:
            # non-agent task: show status + any reason
            t = Text()
            t.append(f"{task.ref}  ", style="bold")
            t.append(f"{task.def_name or task.type}\n", style="grey62")
            t.append(f"status: {task.status}\n", style=fmt.status_color(task.status))
            if task.reason:
                t.append(f"\n{task.reason}", style="red")
            self._body.update(t)
            return
        self._body.update(self._render_snapshot(task, snap))
        self.scroll_end(animate=False)

    def _render_snapshot(self, task: TaskNode, s: AgentSnapshot) -> Text:
        t = Text()
        head = f"{task.ref} · {s.agent or 'agent'}"
        if s.model:
            head += f" · {s.model}"
        t.append(head + "\n", style="bold")
        meta = f"turn {s.num_turns} · {fmt.tokens(s.tokens)} tok · {fmt.cost(s.cost)}"
        if s.elapsed_s is not None:
            meta += f" · {int(s.elapsed_s)}s"
        meta += f" · {s.status}"
        t.append(meta + "\n", style="grey62")
        t.append("─" * 40 + "\n", style="grey37")
        for turn in s.turns:
            n = turn.get("turn", "?")
            cmds = turn.get("commands") or []
            if not cmds:
                txt = (turn.get("text") or "").strip()
                if txt:
                    t.append(f"{n:>3}  ", style="grey50")
                    t.append(txt[:200] + "\n", style="grey70 italic")
                continue
            for i, c in enumerate(cmds):
                t.append(f"{n if i == 0 else '':>3}  ", style="grey50")
                t.append(str(c) + "\n")
        for d in s.denials:
            t.append(f"  DENIED {d}\n", style="red")
        if s.file_changes and not s.running:
            from .result_card import files_section
            t.append("\nfiles\n", style="grey62")
            t.append(files_section([(str(c.get("status") or "•"), str(c["path"]))
                                    for c in s.file_changes]))
        return t
