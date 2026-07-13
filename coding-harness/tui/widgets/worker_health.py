"""Header strip showing Conductor reachability + per-module worker liveness.

The #1 footgun is a run hanging because no worker is polling — this makes it visible
at a glance, everywhere the strip is shown.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from ..api import ConductorClient, ConductorError, PollState


class WorkerHealth(Static):
    def __init__(self, client: ConductorClient):
        super().__init__("", id="worker_health")
        self._client = client

    async def refresh_health(self) -> bool:
        """Poll health; return True if the server is reachable. Never raises."""
        try:
            states = await self._client.health()
            self._render_states(states, server_ok=True)
            return True
        except ConductorError:
            self._render_states({}, server_ok=False)
            return False

    def _render_states(self, states: dict[str, PollState], *, server_ok: bool) -> None:
        t = Text()
        if not server_ok:
            t.append("server ✗ unreachable — is Conductor running?", style="bold red")
            self.add_class("-down")
            self.update(t)
            return

        any_down = False
        t.append("server ✓  ", style="green")
        for module in ("coding_agent", "gitops"):
            st = states.get(module)
            if st and st.alive:
                age = f"{st.age_s:.0f}s" if st.age_s is not None else ""
                t.append(f"· {module} ✓ {age}  ", style="green")
            else:
                any_down = True
                t.append(f"· {module} ✗  ", style="bold red")
        if any_down:
            t.append(" workers down — runs will hang", style="bold red")
            self.add_class("-down")
        else:
            self.remove_class("-down")
        self.update(t)
