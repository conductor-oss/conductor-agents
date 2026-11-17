"""In-app editors for the shared settings files: a prompt template (the /templates library) and the
model policy (/models). They write the same files the web console's Settings page edits, through the
same validated functions (``templates.update``, ``model_profiles.save_user_policy``)."""
from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, Select, Static, TextArea

from .. import model_profiles, templates
from ..widgets.factory_bar import FactoryTopBar


def _split(text: str) -> tuple[str, ...]:
    return tuple(x for x in text.replace(",", " ").split() if x)


class TemplateEditor(Screen):
    """Edit (or create, ``entry`` None) one prompt template in place. ctrl+s saves, ctrl+d loads the
    built-in prompt of the first scoped workflow/field to start from."""

    BINDINGS = [Binding("escape", "back", "back"), Binding("ctrl+s", "save", "save"),
                Binding("ctrl+d", "seed", "built-in prompt")]

    def __init__(self, entry=None, *, on_saved=None):
        super().__init__()
        self._entry = entry
        self._on_saved = on_saved

    def compose(self) -> ComposeResult:
        e = self._entry
        yield FactoryTopBar()
        yield Static(f"Edit template: {e.name}" if e else "New prompt template", id="launcher_title")
        with VerticalScroll():
            yield Label("Name")
            yield Input(e.name if e else "", placeholder="Security-focused review", id="te_name")
            yield Label("Description")
            yield Input(e.description if e else "", placeholder="What this prompt emphasizes", id="te_desc")
            yield Label("Workflows (blank: every workflow that takes templates)")
            yield Input(", ".join(e.workflows) if e else "", id="te_workflows",
                        placeholder=", ".join(templates.WORKFLOW_KEY))
            yield Label("Replaces (prompt fields; blank: each workflow's main prompt)")
            yield Input(", ".join(e.fields) if e else "", id="te_fields", placeholder="reviewPromptTemplate")
            yield Label("Repositories (blank: any)")
            yield Input(", ".join(e.repos) if e else "", id="te_repos", placeholder="owner/name")
            yield Label("Prompt")
            yield TextArea(e.body if e else "", id="te_body", soft_wrap=True, show_line_numbers=True)
            yield Static("", id="te_err", classes="muted")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#te_body", TextArea).styles.height = 24
        (self.query_one("#te_body", TextArea) if self._entry else self.query_one("#te_name", Input)).focus()

    def _value(self, wid: str) -> str:
        return self.query_one(wid, Input).value

    def action_seed(self) -> None:
        workflows, fields = _split(self._value("#te_workflows")), _split(self._value("#te_fields"))
        wf = workflows[0] if workflows else next(iter(templates.WORKFLOW_KEY))
        field = fields[0] if fields else templates.primary_field(wf) or ""
        text = templates.default_prompt(templates.field_key(wf, field) or templates.WORKFLOW_KEY.get(wf))
        if not text:
            self.notify(f"{field or wf} has no built-in prompt", severity="warning")
            return
        self.query_one("#te_body", TextArea).text = text
        self.notify(f"Loaded the built-in prompt for {wf} {field}; edit it from here.")

    def action_save(self) -> None:
        try:
            entry = templates.update(
                self._entry.path if self._entry else None, name=self._value("#te_name"),
                body=self.query_one("#te_body", TextArea).text, description=self._value("#te_desc"),
                workflows=_split(self._value("#te_workflows")), fields=_split(self._value("#te_fields")),
                repos=_split(self._value("#te_repos")))
        except (ValueError, OSError) as exc:
            self.query_one("#te_err", Static).update(f"[red]{exc}[/red]")
            return
        self._entry = entry
        self.notify(f"Saved {entry.path.name}")
        if self._on_saved:
            self._on_saved(entry)

    def action_back(self) -> None:
        self.app.pop_screen()


class RoleModal(ModalScreen):
    """Set the agent and model of one role (or one rescue tier of it)."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, row: dict, backends: list[str], on_save=None):
        super().__init__()
        self._row, self._backends, self._on_save = row, backends, on_save

    def compose(self) -> ComposeResult:
        r = self._row
        tier = "" if r["tier"] is None else (" (first try)" if r["tier"] == 0 else f" (rescue {r['tier']})")
        with Vertical(id="box"):
            yield Label(f"{r['profile']}: {r['role']}{tier}")
            agents = sorted(set(self._backends) | ({r["agent"]} if r["agent"] else set()))
            yield Select([(a, a) for a in agents], value=r["agent"] or agents[0], allow_blank=False, id="rm_agent")
            yield Input(r["model"], placeholder="model, e.g. claude-opus-4-8", id="rm_model")
            yield Static("", id="rm_err", classes="muted")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save", variant="success", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#rm_model", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._submit() if event.button.id == "ok" else self.action_cancel()

    def _submit(self) -> None:
        agent = str(self.query_one("#rm_agent", Select).value)
        model = self.query_one("#rm_model", Input).value.strip()
        error = self._on_save(agent, model) if self._on_save else None
        if error:
            self.query_one("#rm_err", Static).update(f"[red]{error}[/red]")
            return
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()


class PolicyJsonEditor(Screen):
    """The whole model policy as JSON, for fields the table does not show. ctrl+s validates (with the
    workers' rules) and saves; a policy changed elsewhere since it was opened is refused."""

    BINDINGS = [Binding("escape", "back", "back"), Binding("ctrl+s", "save", "save")]

    def __init__(self, on_saved=None):
        super().__init__()
        self._on_saved = on_saved
        self._policy = model_profiles.user_policy()

    def compose(self) -> ComposeResult:
        yield FactoryTopBar()
        yield Static(f"Model policy JSON: {self._policy['path']}  (ctrl+s save, esc back)", id="launcher_title")
        text = json.dumps(self._policy["data"], indent=2) if self._policy.get("data") is not None \
            else self._policy.get("text", "")
        yield TextArea(text, language="json", id="pj_text", show_line_numbers=True)
        yield Static("; ".join(self._policy.get("problems") or []), id="pj_err", classes="muted")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#pj_text", TextArea).focus()

    def action_save(self) -> None:
        err = self.query_one("#pj_err", Static)
        try:
            data = json.loads(self.query_one("#pj_text", TextArea).text)
        except json.JSONDecodeError as exc:
            err.update(f"[red]The JSON does not parse: {exc}[/red]")
            return
        try:
            self._policy = model_profiles.save_user_policy(data, expected_sha256=self._policy.get("sha256") or "")
        except model_profiles.ProfileError as exc:
            err.update(f"[red]{exc}[/red]")
            return
        err.update("")
        self.notify("Model policy saved. New runs use it.")
        if self._on_saved:
            self._on_saved()

    def action_back(self) -> None:
        self.app.pop_screen()


class BuiltinPrompts(Screen):
    """Read the default prompt of every agent step, and which workflows run it. Up and down choose one;
    a repository overrides any of them by committing ``.conductor/<key>.md``."""

    BINDINGS = [Binding("escape", "back", "back")]

    def compose(self) -> ComposeResult:
        self._items = templates.builtins()
        yield FactoryTopBar()
        yield Static("Built-in prompts: up/down to read · esc back", id="launcher_title")
        with Horizontal():
            lv = ListView(*[ListItem(Label(b["key"])) for b in self._items], id="bp_list")
            lv.styles.width = 34
            yield lv
            with Vertical():
                yield Static("", id="bp_info", classes="muted")
                yield TextArea("", id="bp_text", read_only=True, soft_wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#bp_list", ListView).index = 0
        self.query_one("#bp_list", ListView).focus()
        self._show(0)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        index = self.query_one("#bp_list", ListView).index
        if index is not None:
            self._show(index)

    def _show(self, index: int) -> None:
        if not 0 <= index < len(self._items):
            return
        item = self._items[index]
        used = ", ".join(item["usedBy"]) or "no workflow"
        self.query_one("#bp_info", Static).update(
            f"{item['key']}: runs in {used}. Override per repository with {item['repoOverride']}.")
        self.query_one("#bp_text", TextArea).text = item["body"]

    def action_back(self) -> None:
        self.app.pop_screen()
