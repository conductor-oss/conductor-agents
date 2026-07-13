"""Terminal-state result card: the outcome + the link you want, one keypress away."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from .. import catalog, format as fmt
from ..api import RunDetail

_STATUS_STYLE = {"A": "green", "M": "yellow", "D": "red", "R": "cyan", "•": "grey62"}
_MAX_FILES = 15


def files_section(changes: list[tuple[str, str]], max_rows: int = _MAX_FILES) -> Text:
    """Render '(status) path' rows — A green, M yellow, D red — capped with a +N more."""
    t = Text()
    for status, path in changes[:max_rows]:
        t.append(f"  {status}  ", style=_STATUS_STYLE.get(status, "grey62"))
        t.append(f"{path}\n")
    extra = len(changes) - max_rows
    if extra > 0:
        t.append(f"  … +{extra} more\n", style="grey62")
    return t


class ResultCard(Static):
    def __init__(self):
        super().__init__("", id="result_card")

    def show(self, detail: RunDetail) -> str | None:
        """Render the card; return the primary URL (what `o` opens), if any."""
        run = detail.run
        card = catalog.result_for(run.workflow, run.output)
        tokens, cost = detail.tokens_cost()
        t = Text()
        t.append(f"{fmt.status_glyph(run.status)} {run.status}", style=fmt.status_color(run.status))
        t.append(f"  ·  {fmt.duration(run.duration_ms(run.end_ms))}  ·  "
                 f"{fmt.tokens(tokens)} tok  ·  {fmt.cost(cost)}\n\n", style="grey62")

        if card is None:
            t.append(run.workflow + " finished.\n", style="bold")
            if run.reason:
                t.append(f"\n{run.reason}\n", style="red")
            self.update(t)
            return None

        t.append(card.title + "\n", style="bold")
        for label, value in card.rows:
            t.append(f"  {label:<16}", style="grey62")
            t.append(f"{value}\n")
        changes = detail.file_changes()
        if changes:
            t.append("\nfiles\n", style="grey62")
            t.append(files_section(changes))
        url = card.primary_url
        if url:
            t.append(f"\n{url}\n", style="cyan underline")
            t.append(f"\n[o] {card.primary_label}   ", style="grey62")
        else:
            t.append("\n", style="")
        if changes:
            t.append("[f] open a file   ", style="grey62")
        t.append("[e] open folder   [n] run again   [esc] back", style="grey62")
        if run.reason and run.status != "COMPLETED":
            t.append(f"\n\n{run.reason}", style="red")
        self.update(t)
        return url
