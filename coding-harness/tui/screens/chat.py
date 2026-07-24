"""Chat — the conversational front door. An LLM agent drives the harness by calling
tools; the user talks to it here. Chat is the TUI's default landing screen."""

from __future__ import annotations

import json

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Input, Static

from .. import format as fmt
from ..api import ConductorError
from ..chat import tools
from ..chat.llm import ChatEngine
from ..chat.prompt import system_prompt
from ..widgets.factory_bar import FactoryTopBar
from ..widgets.modals import ConfirmModal


class Chat(Screen):
    BINDINGS = [
        Binding("ctrl+o", "open_last", "open run", show=True),
    ]

    def __init__(self):
        super().__init__()
        self._last_run_id: str | None = None
        self._cur_assistant: Static | None = None
        self._cur_text = ""
        self._busy = False
        self._tokens = 0
        self._cost = 0.0

    def compose(self) -> ComposeResult:
        yield FactoryTopBar()
        yield Static("", id="chat_header")
        yield VerticalScroll(id="transcript")
        yield Input(placeholder="Ask the harness…  (/help for commands)", id="chat_input")
        yield Footer()

    def on_mount(self) -> None:
        self._render_header()
        self._render_history()
        if self.app.llm_client is None:
            self._bubble("system",
                         "Chat needs ANTHROPIC_API_KEY set in this environment. "
                         "Set it and relaunch, or use /dashboard to drive the harness by forms.")
        else:
            self._bubble("system", "Ask me to review a PR, resolve an issue, or check on a run. "
                                    "Type /help for commands.")
        self.query_one("#chat_input", Input).focus()

    # ------------------------------------------------------------------ rendering
    def _render_header(self) -> None:
        s = self.app.session
        t = Text()
        t.append("chat ", style="bold")
        t.append(f"· {self.app.settings.model} ", style="grey62")
        t.append(f"· {s.title} ", style="grey70")
        if self._tokens:
            t.append(f"· {fmt.tokens(self._tokens)} tok {fmt.cost(self._cost)} ", style="grey62")
        if s.runs:
            t.append(f"· {len(s.runs)} run(s)", style="grey62")
        self.query_one("#chat_header", Static).update(t)

    def _render_history(self) -> None:
        for m in self.app.session.messages:
            role, content = m.get("role"), m.get("content")
            if role == "user" and isinstance(content, str):
                self._bubble("you", content)
            elif role == "assistant" and isinstance(content, list):
                for b in content:
                    if b.get("type") == "text" and b.get("text"):
                        self._bubble("assistant", b["text"])
                    elif b.get("type") == "tool_use":
                        self._tool_line(b.get("name", "?"), b.get("input") or {})

    def _bubble(self, role: str, text: str) -> Static:
        styles = {"you": ("you", "cyan"), "assistant": ("", "white"),
                  "system": ("", "grey50"), "tool": ("", "grey50")}
        prefix, color = styles.get(role, ("", "white"))
        body = Text()
        if prefix:
            body.append(f"{prefix} ", style="bold cyan")
        body.append(text, style=color)
        w = Static(body, classes=f"bubble bubble-{role}")
        self.query_one("#transcript", VerticalScroll).mount(w)
        self.call_after_refresh(self._scroll_end)
        return w

    def _tool_line(self, name: str, inp: dict) -> None:
        compact = json.dumps(inp, separators=(",", ":"))
        if len(compact) > 80:
            compact = compact[:80] + "…"
        self._bubble("tool", f"▶ {name} {compact}")

    def _scroll_end(self) -> None:
        self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)

    # ------------------------------------------------------------------ input
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self._slash(text)
            return
        if self.app.llm_client is None:
            self._bubble("system", "No LLM configured — set ANTHROPIC_API_KEY, or use /dashboard.")
            return
        if self._busy:
            self._bubble("system", "…still working on the previous message.")
            return
        self._bubble("you", text)
        self.app.session.messages.append({"role": "user", "content": text})
        self.app.session.set_title_from(text)
        self.app.session_store.save(self.app.session)
        self._render_header()
        self.run_turn()

    def _slash(self, text: str) -> None:
        cmd, _, arg = text[1:].partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ("d", "dashboard"):
            from .dashboard import Dashboard
            self.app.push_screen(Dashboard())
        elif cmd in ("s", "sessions"):
            from .sessions import Sessions
            self.app.push_screen(Sessions())
        elif cmd in ("templates", "t"):
            from .templates import TemplatesScreen
            self.app.push_screen(TemplatesScreen())
        elif cmd in ("models", "m"):
            from .model_profiles import ModelProfiles
            self.app.push_screen(ModelProfiles())
        elif cmd in ("o", "open"):
            self.action_open_last(arg or None)
        elif cmd in ("folder", "edit"):
            self.open_folder(arg or self._last_run_id)
        elif cmd in ("register", "update-workflows"):
            self.register_workflows()
        elif cmd == "new":
            from ..chat.session import Session
            self.app.session = Session.new(self.app.settings.model)
            self.query_one("#transcript", VerticalScroll).remove_children()
            self._tokens = self._cost = 0
            self._render_header()
            self._bubble("system", "Started a new session.")
        elif cmd in ("q", "quit"):
            self.app.exit()
        elif cmd == "help":
            self._bubble("system",
                         "/dashboard   open the fleet dashboard\n"
                         "/sessions    browse & resume past chats\n"
                         "/templates   manage prompt templates (edit/new/delete)\n"
                         "/models      manage workflow model profiles\n"
                         "/open [id]   open a run's live view (last started if no id)\n"
                         "/folder [id] open a run's working folder in your editor\n"
                         "/register    update task + workflow definitions on this server\n"
                         "/new         start a fresh session\n"
                         "/quit        exit  (ctrl+o also opens the last run)")
        else:
            self._bubble("system", f"unknown command /{cmd} — try /help")

    # ------------------------------------------------------------------ the turn
    @work(exclusive=True, group="chat")
    async def run_turn(self) -> None:
        self._busy = True
        self._cur_assistant = None
        self._cur_text = ""
        engine = ChatEngine(self.app.llm_client, self.app.settings.model,
                            system_prompt(self.app.settings.server_url))

        def on_text(chunk: str) -> None:
            if self._cur_assistant is None:
                self._cur_assistant = self._bubble("assistant", "")
                self._cur_text = ""
            self._cur_text += chunk
            self._cur_assistant.update(Text(self._cur_text))
            self.call_after_refresh(self._scroll_end)

        def on_tool_start(name: str, inp: dict) -> None:
            self._cur_assistant = None          # next text starts a fresh bubble
            self._tool_line(name, inp)

        def on_tool_done(name: str, out: str) -> None:
            snippet = out if len(out) <= 200 else out[:200] + "…"
            self._bubble("tool", f"  → {snippet}")

        # One context per user turn: its start guard survives every tool-use round trip the
        # model emits, so the host can enforce at most one workflow start for this turn.
        ctx = tools.ToolContext(client=self.app.client, confirm=self._confirm,
                                on_run_started=self._on_run_started,
                                server_url=self.app.settings.server_url)

        async def run_tool(name: str, inp: dict) -> str:
            return await tools.dispatch(name, inp, ctx)

        try:
            res = await engine.run(
                self.app.session.messages, run_tool=run_tool, on_text=on_text,
                on_tool_start=on_tool_start, on_tool_done=on_tool_done)
            self._tokens += res["tokens"]
            self._cost += res["cost"]
        except Exception as e:  # noqa: BLE001
            self._bubble("system", f"error: {type(e).__name__}: {e}")
        finally:
            self._busy = False
            self.app.session_store.save(self.app.session)
            self._render_header()

    async def _confirm(self, title: str, message: str) -> bool:
        return bool(await self.app.push_screen_wait(
            ConfirmModal(title, message, confirm_label="Confirm")))

    @work(exclusive=True, group="registration")
    async def register_workflows(self) -> None:
        confirmed = await self._confirm(
            "Update workflow definitions",
            f"Register all harness task and workflow definitions on\n"
            f"{self.app.settings.server_url}?",
        )
        if not confirmed:
            self._bubble("system", "workflow registration cancelled.")
            return
        self._bubble("system", "Registering task and workflow definitions…")
        from ..registration import register_definitions
        result = await register_definitions(self.app.settings.server_url)
        if result.ok:
            self._bubble("system", f"Registration complete.\n{result.output}")
        else:
            self._bubble("system", f"Registration failed.\n{result.output}")

    def _on_run_started(self, workflow_id: str) -> None:
        self.app.session.add_run(workflow_id)
        self.app.track(workflow_id)
        self._last_run_id = workflow_id
        self._render_header()

    def reload_session(self) -> None:
        """Re-render for a newly-resumed session (called by the Sessions picker)."""
        self.query_one("#transcript", VerticalScroll).remove_children()
        self._tokens = self._cost = 0
        self._last_run_id = self.app.session.runs[-1] if self.app.session.runs else None
        self._render_header()
        self._render_history()
        self._bubble("system", f"Resumed session · {self.app.session.title}")
        self.query_one("#chat_input", Input).focus()

    def action_open_last(self, run_id: str | None = None) -> None:
        wid = run_id or self._last_run_id
        if not wid:
            self._bubble("system", "no run to open yet — start one, or /dashboard to browse.")
            return
        from .run_detail import RunDetail
        self.app.track(wid)
        self.app.push_screen(RunDetail(wid))

    @work(group="folder")
    async def open_folder(self, run_id: str | None) -> None:
        import os
        from .. import edit
        from ..api import ConductorError
        if not run_id:
            self._bubble("system", "no run yet — start one first, then /folder.")
            return
        try:
            detail = await self.app.client.get_run(run_id, recurse=False)
            path = detail.workspace()
        except ConductorError:
            path = None
        if not path or not os.path.isdir(path):
            self._bubble("system", "working dir not on this host (remote workers, or a cleaned /tmp clone).")
            return
        self._bubble("system", edit.open_path(self.app, path, self.app.settings.editor))
