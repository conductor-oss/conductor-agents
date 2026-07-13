"""Best-effort desktop notification + terminal bell on terminal run states.

Fires only for runs started/opened this session (the caller decides), and only when
enabled. Never raises — a missing notifier is silently skipped.

Click-to-open: on macOS, `terminal-notifier` (if installed: `brew install terminal-notifier`)
posts a notification whose click opens the run's URL. Plain `osascript display notification`
cannot attach a click action — such notifications are owned by Script Editor and clicking
them just opens Script Editor — so it's used only as a non-clickable fallback.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def notify(enabled: bool, title: str, message: str, url: str | None = None) -> None:
    if not enabled:
        return
    try:
        sys.stdout.write("\a")   # terminal bell
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        if sys.platform == "darwin" and shutil.which("terminal-notifier"):
            args = ["terminal-notifier", "-title", title, "-message", message]
            if url:
                args += ["-open", url]        # clicking opens the run in the browser
            subprocess.run(args, check=False, capture_output=True, timeout=5)
        elif sys.platform == "darwin" and shutil.which("osascript"):
            body = message.replace('"', "'")
            ttl = title.replace('"', "'")
            subprocess.run(
                ["osascript", "-e", f'display notification "{body}" with title "{ttl}"'],
                check=False, capture_output=True, timeout=5,
            )
        elif shutil.which("notify-send"):
            # notify-send has no portable click-to-open; show the URL in the body instead.
            body = f"{message}\n{url}" if url else message
            subprocess.run(["notify-send", title, body], check=False,
                           capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001 — notification must never break the app
        pass
