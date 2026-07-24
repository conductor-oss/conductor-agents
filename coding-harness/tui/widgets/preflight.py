"""Launcher preflight: server reachable · workflow registered · workers polling.

Mirrors SKILL.md's preflight — each ✗ carries its one-line fix. Server/def failures
block Start; stale workers warn (the run would hang) but don't hard-block.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from ..api import ConductorClient, ConductorError


class Preflight(Static):
    def __init__(self, client: ConductorClient):
        super().__init__("", id="preflight")
        self._client = client
        self.ok_to_start = False

    async def check(self, workflow_name: str) -> bool:
        """Run the three checks; set ok_to_start; render. Returns ok_to_start."""
        server_ok = def_ok = workers_ok = False
        try:
            states = await self._client.health()      # also proves the server is reachable
            server_ok = True
            workers_ok = all(s.alive for s in states.values()) and bool(states)
        except ConductorError:
            states = {}
        if server_ok:
            def_ok = await self._client.workflow_registered(workflow_name)

        t = Text("Preflight  ")
        t.append("✓ server  " if server_ok else "✗ server  ",
                 style="green" if server_ok else "bold red")
        t.append("✓ registered  " if def_ok else "✗ registered  ",
                 style="green" if def_ok else "bold red")
        t.append("✓ workers" if workers_ok else "⚠ workers",
                 style="green" if workers_ok else "yellow")
        if not server_ok:
            t.append("\n  server unreachable — is Conductor running / --server correct?", style="red")
        elif not def_ok:
            t.append(
                f"\n  {workflow_name} is missing or stale — use /register in chat or g on the dashboard",
                style="red",
            )
        elif not workers_ok:
            t.append("\n  workers not polling — run `python main.py`; the run will hang until they do",
                     style="yellow")
        self.update(t)
        self.set_class(server_ok and def_ok, "-ok")
        self.set_class(not (server_ok and def_ok), "-bad")
        self.ok_to_start = server_ok and def_ok       # workers stale only warns
        return self.ok_to_start
