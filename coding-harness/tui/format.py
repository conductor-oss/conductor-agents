"""Display helpers: humanized tokens/cost/durations and status glyphs/styles."""

from __future__ import annotations

# Status → (glyph, Textual/Rich color). NEEDS_YOU reserved for v2.
_STATUS = {
    "RUNNING": ("▶", "dodger_blue2"),
    "SCHEDULED": ("▶", "dodger_blue2"),
    "IN_PROGRESS": ("▶", "dodger_blue2"),
    "COMPLETED": ("✓", "green"),
    "FAILED": ("✗", "red"),
    "FAILED_WITH_TERMINAL_ERROR": ("✗", "red"),
    "TIMED_OUT": ("✗", "red"),
    "TERMINATED": ("◼", "grey62"),
    "CANCELED": ("◼", "grey62"),
    "PAUSED": ("‖", "yellow"),
}
_PENDING = ("○", "grey42")


def status_glyph(status: str) -> str:
    return _STATUS.get((status or "").upper(), _PENDING)[0]


def status_color(status: str) -> str:
    return _STATUS.get((status or "").upper(), _PENDING)[1]


def status_label(status: str) -> str:
    return (status or "?").upper()


def tokens(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def cost(f) -> str:
    try:
        return f"${float(f or 0):.2f}"
    except (TypeError, ValueError):
        return "—"


def duration(ms) -> str:
    if ms is None:
        return "—"
    s = max(0, int(ms // 1000))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def short_id(wid: str) -> str:
    return (wid or "")[:8]
