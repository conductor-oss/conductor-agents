"""Launcher — kick off new work: pick an action, fill a catalog-driven form, start.

`Launcher` is the action picker; `LauncherForm` is the per-workflow form. Callers push
`Launcher()` for a fresh start, or `LauncherForm(name, values)` directly to re-run with
a run's previous input prefilled.
"""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (Button, Collapsible, Footer, Input, Label, ListItem,
                             ListView, Select, Static, Switch, TextArea)

from .. import catalog, gh
from ..api import ConductorError
from ..catalog import Field
from ..widgets.modals import PickerModal
from ..widgets.preflight import Preflight


class Launcher(Screen):
    BINDINGS = [Binding("escape", "back", "back")]

    def compose(self) -> ComposeResult:
        yield Label("New run — pick an action", id="launcher_title")
        lv = ListView(id="action_list")
        yield lv
        yield Footer()

    def on_mount(self) -> None:
        lv = self.query_one("#action_list", ListView)
        for name in catalog.LAUNCHABLE:
            spec = catalog.CATALOG[name]
            item = ListItem(Label(f"{spec.action}\n  [dim]{spec.blurb}[/dim]"))
            item.data = name
            lv.append(item)
        if len(lv):
            lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        name = getattr(event.item, "data", None)
        if name:
            self.app.push_screen(LauncherForm(name))

    def action_back(self) -> None:
        self.app.pop_screen()


class LauncherForm(Screen):
    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("ctrl+s", "start", "start"),
    ]

    def __init__(self, name: str, values: dict | None = None):
        super().__init__()
        self.spec = catalog.CATALOG[name]
        self._init = dict(values or {})
        self._widgets: dict[str, object] = {}

    def compose(self) -> ComposeResult:
        yield Label(f"{self.spec.action}  [dim]({self.spec.name})[/dim]", id="launcher_title")
        with VerticalScroll():
            common = [f for f in self.spec.fields if not f.advanced]
            advanced = [f for f in self.spec.fields if f.advanced]
            for f in common:
                yield from self._row(f)
            # Prompt-template picker (visible), if this workflow has a template field. The
            # editable text lives in the Advanced TextArea; this Select drives it.
            if self._template_field():
                yield Horizontal(Label("Prompt template"),
                                 Select([("Built-in default", "__builtin__")],
                                        value="__builtin__", allow_blank=False, id="tplsel"),
                                 classes="field-row")
                yield Label("", id="tpl_hint", classes="field-help")
            if advanced:
                with Collapsible(title="Advanced", collapsed=True):
                    for f in advanced:
                        yield from self._row(f)
            yield Preflight(self.app.client)
            with Horizontal(classes="modal-buttons"):
                yield Button("Start ▶", variant="success", id="start")
        yield Static("", id="form_error", classes="banner-error")
        yield Footer()

    def _row(self, f: Field):
        w = self._make_widget(f)
        self._widgets[f.name] = w
        if f.kind == "template":
            # editor for the chosen prompt; the visible picker (Select) above drives it
            yield Horizontal(
                Label(f"{f.label} (edit)"),
                Button("Load default", id=f"tpldef_{f.name}"),
                Button("Save as…", id=f"tplsave_{f.name}"),
                classes="field-row",
            )
            yield w
            if f.help:
                yield Label(f.help, classes="field-help")
            return
        row = Horizontal(Label(f"{f.label}{'*' if f.required else ''}"), w, classes="field-row")
        if f.kind in ("gh_issue", "gh_pr"):
            row = Horizontal(
                Label(f"{f.label}{'*' if f.required else ''}"), w,
                Button("browse", id=f"browse_{f.name}"),
                classes="field-row",
            )
        yield row
        if f.help:
            yield Label(f.help, classes="field-help")

    def _make_widget(self, f: Field):
        init = self._init.get(f.name, f.form_default)
        if f.kind == "bool":
            return Switch(value=self._as_bool(init))
        if f.kind == "enum":
            opts = [(c, c) for c in f.choices]
            val = init if init in f.choices else (f.default if f.default in f.choices else Select.BLANK)
            return Select(opts, value=val, allow_blank=True)
        if f.kind in ("multiline", "template"):
            return TextArea(str(init or ""), id=f"w_{f.name}")
        typ = "integer" if f.kind in ("int", "gh_issue", "gh_pr") else ("number" if f.kind == "float" else "text")
        return Input(value="" if init in (None, "") else str(init),
                     type=typ, id=f"w_{f.name}")

    @staticmethod
    def _as_bool(v) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def on_mount(self) -> None:
        self.query_one("#form_error", Static).display = False
        self.run_preflight()
        if self._template_field():
            self._rebuild_tpl_picker()   # populate + auto-select the prompt-template picker
        # Focus the first field so you can start typing immediately.
        if self.spec.fields:
            first = self._widgets.get(self.spec.fields[0].name)
            if first is not None:
                self.call_after_refresh(first.focus)

    # ------------------------------------------------------------------ template picker
    def _template_field(self):
        return next((f for f in self.spec.fields if f.kind == "template"), None)

    def _target_repo(self) -> str:
        """The repo the run targets, for repo-scoped template filtering. Empty string when the
        repo field is blank or the workflow is local (→ only unrestricted templates apply).
        Never None — None is reserved for the manager's unfiltered listing."""
        if any(f.name == "repo" for f in self.spec.fields):
            return self._value("repo") or ""
        return ""

    def _rebuild_tpl_picker(self) -> None:
        """(Re)populate the visible template Select for the current workflow + target repo.
        Auto-selects the sole applicable template; with several, defaults to Built-in so the
        user picks; preserves a still-valid current selection across repo changes."""
        from .. import templates
        f = self._template_field()
        if not f:
            return
        entries = templates.list_templates(self.spec.name, repo=self._target_repo())
        self._tpl_entries = {str(e.path): e for e in entries}
        sel = self.query_one("#tplsel", Select)
        opts = [("Built-in default", "__builtin__")] + [(e.name, str(e.path)) for e in entries]
        opts.append(("Custom (edit below)", "__custom__"))
        prev = sel.value
        sel.set_options(opts)
        # decide the selection
        if prev in self._tpl_entries or prev == "__custom__":
            sel.value = prev                              # keep a still-valid choice
        elif len(entries) == 1:
            sel.value = str(entries[0].path)              # exactly one → auto-select it
        else:
            sel.value = "__builtin__"
        self._apply_selection(sel.value)
        self._set_tpl_hint(len(entries))

    def _apply_selection(self, value) -> None:
        """Drive the (Advanced) TextArea from the picker choice."""
        from .. import templates
        f = self._template_field()
        w = self._widgets.get(f.name) if f else None
        if not isinstance(w, TextArea):
            return
        if value == "__custom__":
            return                                        # leave whatever the user typed
        if value in ("__builtin__", Select.BLANK, None):
            w.text = ""                                   # empty → built-in / repo default used
        else:
            entry = self._tpl_entries.get(value)
            if entry is not None:
                w.text = templates.load(entry)

    def _set_tpl_hint(self, n: int) -> None:
        f = self._template_field()
        repo = self._target_repo()
        where = f" for {self.spec.name}" + (f" · {repo}" if repo else "")
        if n == 1:
            msg = f"1 template{where} — auto-selected (edit under Advanced, or pick Built-in)"
        elif n > 1:
            msg = f"{n} templates{where} — pick one, or Built-in"
        else:
            msg = f"no saved templates{where} — Advanced ▸ {f.label} ▸ Save as… to create one"
        self.query_one("#tpl_hint", Label).update(msg)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "tplsel":
            self._apply_selection(event.value)

    # ------------------------------------------------------------------ preflight
    @work(exclusive=True, group="preflight")
    async def run_preflight(self) -> None:
        await self.query_one(Preflight).check(self.spec.name)

    def on_input_changed(self, event: Input.Changed) -> None:
        # when the target repo changes, re-filter the template picker (repo-scoped templates)
        if event.input.id == "w_repo" and self._template_field():
            self._rebuild_tpl_picker()

    # ------------------------------------------------------------------ gh pickers
    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "start":
            self.action_start()
        elif bid.startswith("browse_"):
            self._browse(bid[len("browse_"):])
        elif bid.startswith("tpldef_"):
            self._load_default(bid[len("tpldef_"):])
        elif bid.startswith("tplsave_"):
            self._save_template(bid[len("tplsave_"):])

    # ------------------------------------------------------------------ prompt templates
    def _load_default(self, field_name: str) -> None:
        """Fill the editor with the shipped built-in prompt text so the user can tweak it;
        marks the picker Custom (this becomes an explicit, sent prompt)."""
        from .. import templates
        text = templates.default_prompt(templates.FIELD_KEY.get(field_name))
        w = self._widgets.get(field_name)
        if text and isinstance(w, TextArea):
            w.text = text
            try:
                self.query_one("#tplsel", Select).value = "__custom__"
            except Exception:  # noqa: BLE001
                pass
            self.notify("loaded the built-in default to edit — Start to use it")
        else:
            self._error("no built-in default available for this field.")

    def _save_template(self, field_name: str) -> None:
        from ..widgets.modals import SaveTemplateModal
        w = self._widgets.get(field_name)
        text = w.text if isinstance(w, TextArea) else ""
        if not text.strip():
            self._error("nothing to save — type or load a prompt first.")
            return
        self.app.push_screen(SaveTemplateModal(
            self.spec.name, self._target_repo(),
            on_save=lambda name, scoped, repos: self._do_save_template(name, text, scoped, repos)))

    def _do_save_template(self, name: str, text: str, scoped: bool, repos: tuple[str, ...]) -> None:
        from .. import templates
        wfs = (self.spec.name,) if scoped else ()
        path = templates.save(name, text, workflows=wfs, repos=repos)
        self.notify(f"saved template → {path.name}")
        self._rebuild_tpl_picker()   # the new one shows up (and auto-selects if it's now the only match)

    def _browse(self, field_name: str) -> None:
        repo = self._value("repo")
        if not repo:
            self._error("Enter the repo first, then browse.")
            return
        field = next((f for f in self.spec.fields if f.name == field_name), None)
        if field:
            self._load_picker(field, repo)

    @work(group="gh")
    async def _load_picker(self, field: Field, repo: str) -> None:
        items = await (gh.list_prs(repo) if field.kind == "gh_pr" else gh.list_issues(repo))
        if not items:
            self._error("gh unavailable or no open items — type the number instead.")
            return
        kind = "PR" if field.kind == "gh_pr" else "issue"
        self.app.push_screen(PickerModal(f"Pick a {kind}", items,
                                         on_pick=lambda n: self._set(field.name, n)))

    def _set(self, name: str, value) -> None:
        w = self._widgets.get(name)
        if isinstance(w, Input):
            w.value = str(value)

    # ------------------------------------------------------------------ start
    def action_start(self) -> None:
        self.start_run()

    @work(exclusive=True, group="start")
    async def start_run(self) -> None:
        ok = await self.query_one(Preflight).check(self.spec.name)
        if not ok:
            self._error("Preflight failed — see above.")
            return
        try:
            values, missing = self._collect()
        except ValueError as e:
            self._error(str(e))
            return
        if missing:
            self._error("Required: " + ", ".join(missing))
            return
        payload = self.spec.build_payload(values)
        try:
            wid = await self.app.client.start(self.spec.name, payload)
        except ConductorError as e:
            self._error(f"Start failed: {e}")
            return
        self.app.track(wid)
        from .run_detail import RunDetail
        self.app.switch_screen(RunDetail(wid))

    def _collect(self) -> tuple[dict, list[str]]:
        values, missing = {}, []
        for f in self.spec.fields:
            raw = self._value(f.name)
            if f.kind == "bool":
                values[f.name] = bool(raw)
                continue
            if raw in (None, "", Select.BLANK):
                if f.required:
                    missing.append(f.label)
                else:
                    values[f.name] = f.default
                continue
            if f.kind in ("int", "gh_issue", "gh_pr"):
                try:
                    values[f.name] = int(raw)
                except (TypeError, ValueError):
                    raise ValueError(f"{f.label} must be a number")
            elif f.kind == "float":
                try:
                    values[f.name] = float(raw)
                except (TypeError, ValueError):
                    raise ValueError(f"{f.label} must be a number")
            else:
                values[f.name] = raw
        return values, missing

    def _value(self, name: str):
        w = self._widgets.get(name)
        if isinstance(w, Switch):
            return w.value
        if isinstance(w, Select):
            return w.value
        if isinstance(w, TextArea):
            return w.text
        if isinstance(w, Input):
            return w.value
        return None

    def _error(self, msg: str) -> None:
        e = self.query_one("#form_error", Static)
        e.update(msg)
        e.display = bool(msg)

    def action_back(self) -> None:
        self.app.pop_screen()
