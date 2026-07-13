"""Entry point: `python -m tui [--model M] [--server URL] [--resume [ID]] [--dashboard] [--no-notify]`.

Run from the repo root (python -m tui resolves the `tui` package from sys.path).
"""

from __future__ import annotations

import argparse

from . import config
from .app import HarnessApp


def main() -> None:
    p = argparse.ArgumentParser(prog="tui", description="Conductor Coding Harness TUI")
    p.add_argument("--model", help="chat driver model (default: sonnet; aliases: sonnet/opus/haiku, "
                                   "or a full id like claude-sonnet-4-6)")
    p.add_argument("--server", help="Conductor API base URL (default: $CONDUCTOR_SERVER_URL "
                                    "or http://localhost:8080/api)")
    p.add_argument("--resume", nargs="?", const="last", default=None,
                   help="resume a chat session: --resume (last) or --resume <session-id>")
    p.add_argument("--dashboard", action="store_true", help="land on the dashboard instead of chat")
    p.add_argument("--editor", help="command to open a run's working folder "
                                    "(default: $VISUAL/$EDITOR, else code/cursor/…, else OS open)")
    p.add_argument("--no-notify", action="store_true", help="disable terminal bell + OS notifications")
    args = p.parse_args()
    settings = config.load(server=args.server, notify=not args.no_notify, model=args.model,
                           editor=args.editor)
    HarnessApp(settings, resume=args.resume, start_dashboard=args.dashboard).run()


if __name__ == "__main__":
    main()
