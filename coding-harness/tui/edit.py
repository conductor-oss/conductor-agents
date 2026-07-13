"""Open a path in the user's external editor / IDE — the bridge from the TUI to real tools.

Resolution order: explicit override (--editor / $CONDUCTOR_HARNESS_EDITOR) → $VISUAL →
$EDITOR → a detected GUI launcher → the OS "open" command. GUI/OS launchers detach (we
Popen them); a terminal editor (vim/nano from $EDITOR) is run inside `App.suspend()` so it
takes over the terminal cleanly and the TUI resumes afterwards. Never raises.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

# GUI editors that open a window and return immediately (given a folder or file).
_GUI = ["code", "code-insiders", "cursor", "subl", "zed", "idea", "windsurf"]


def _os_open() -> list[str] | None:
    if sys.platform == "darwin" and shutil.which("open"):
        return ["open"]
    if shutil.which("xdg-open"):
        return ["xdg-open"]
    return None


def resolve_editor(override: str | None = None) -> tuple[list[str] | None, bool]:
    """Return (command-prefix, is_gui). command-prefix is a list to which the path is
    appended. is_gui → the launcher detaches (Popen); else run under suspend()."""
    for candidate in (override, os.environ.get("VISUAL"), os.environ.get("EDITOR")):
        if candidate and candidate.strip():
            parts = shlex.split(candidate)
            base = os.path.basename(parts[0])
            return parts, base in _GUI
    for name in _GUI:
        if shutil.which(name):
            return [name], True
    os_open = _os_open()
    if os_open:
        return os_open, True
    return None, False


def open_path(app, path: str, override: str | None = None) -> str:
    """Open ``path`` in the resolved editor. Returns a short status for the caller to toast."""
    cmd, is_gui = resolve_editor(override)
    if not cmd:
        return "no editor found — set $EDITOR or --editor"
    argv = [*cmd, path]
    try:
        if is_gui:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"opened in {os.path.basename(cmd[0])}"
        # terminal editor: hand it the terminal, then resume the TUI
        if hasattr(app, "suspend"):
            with app.suspend():
                subprocess.run(argv)
            return f"opened in {os.path.basename(cmd[0])}"
        subprocess.Popen(argv)   # fallback if suspend unavailable
        return f"launched {os.path.basename(cmd[0])}"
    except Exception as e:  # noqa: BLE001
        return f"could not open editor: {e}"
