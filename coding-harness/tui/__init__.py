"""Harness TUI — a terminal interface for driving the Conductor coding harness.

A read-mostly client of the Conductor REST API (the same API the workers and the
`conductor` CLI use). It starts / terminates / retries workflows and renders their
live state. It changes no worker, workflow, or server code.

Entry point: ``python -m tui`` (see ``__main__``).
"""

__version__ = "0.1.0"
