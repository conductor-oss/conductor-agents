"""The task tree: a run's tasks in execution order, recursing into sub-workflows.

Rebuilt each poll (bounded depth ≤ 2, few tasks) with everything expanded — the
"watch all forks at once" view. The screen tracks the selected task by ref, so the
right-hand agent pane stays stable across rebuilds.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Tree

from .. import format as fmt
from ..api import TaskNode

_SPARK = "▁▂▃▄▅▆▇█"


def _spark(n: int) -> str:
    return _SPARK[min(n, len(_SPARK) - 1)] if n else ""


def _label(t: TaskNode) -> Text:
    lbl = Text()
    lbl.append(f"{fmt.status_glyph(t.status)} ", style=fmt.status_color(t.status))
    lbl.append(t.ref, style="bold")
    name = t.def_name or t.type
    if name and name != t.ref:
        lbl.append(f"  {name}", style="grey62")
    snap = t.snapshot()
    if snap:
        if snap.running:
            lbl.append(f"  {_spark(snap.num_turns)} {snap.num_turns} turns", style="dodger_blue2")
        else:
            lbl.append(f"  {snap.num_turns} turns · {fmt.tokens(snap.tokens)}", style="grey62")
    elif t.type == "SUB_WORKFLOW":
        lbl.append(f"  [{t.status.lower()}]", style="grey62")
    return lbl


class TaskTree(Tree):
    def __init__(self):
        super().__init__("tasks", id="task_tree")
        self.show_root = False
        self.guide_depth = 3

    def sync(self, tasks: list[TaskNode]) -> None:
        self.clear()
        for t in tasks:
            self._add(self.root, t)
        self.root.expand_all()

    def _add(self, parent, t: TaskNode) -> None:
        node = parent.add(_label(t), data=t, expand=True)
        for child in t.children:
            self._add(node, child)
