"""Modal overlays: task logs viewer, yes/no confirm, pickers, and the HITL approval gate."""

from __future__ import annotations

import json
import os
import tempfile

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog, Static


class LogsModal(ModalScreen):
    BINDINGS = [Binding("escape,q,l", "dismiss", "close")]

    def __init__(self, title: str, lines: list[str]):
        super().__init__()
        self._title = title
        self._lines = lines or ["(no logs)"]

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        with Vertical(id="box"):
            yield Label(f"Logs — {self._title}")
            log = RichLog(id="log_body", wrap=True, highlight=False, markup=False)
            yield log
            with Horizontal(classes="modal-buttons"):
                yield Button("Close", variant="primary", id="close")

    def on_mount(self) -> None:
        log = self.query_one("#log_body", RichLog)
        for ln in self._lines:
            log.write(ln)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_dismiss()

    def action_dismiss(self) -> None:
        self.app.pop_screen()


class PickerModal(ModalScreen):
    """Pick one item from a list (used for gh issue/PR selection). Calls on_pick(value)."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title: str, items: list[tuple[int, str]], on_pick=None):
        super().__init__()
        self._title = title
        self._items = items
        self._on_pick = on_pick

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        from textual.widgets import ListItem, ListView
        with Vertical(id="box"):
            yield Label(self._title)
            lv = ListView(id="picker_list")
            yield lv

    def on_mount(self) -> None:
        from textual.widgets import ListItem, ListView
        lv = self.query_one("#picker_list", ListView)
        for num, title in self._items:
            item = ListItem(Label(f"#{num}  {title[:70]}"))
            item.data = num
            lv.append(item)
        if len(self._items):
            lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event) -> None:
        num = getattr(event.item, "data", None)
        self.app.pop_screen()
        if num is not None and self._on_pick:
            self._on_pick(num)

    def action_cancel(self) -> None:
        self.app.pop_screen()


class FileListModal(ModalScreen):
    """Changed-files picker: rows '<status>  <path>'; enter calls on_pick(path)."""

    BINDINGS = [Binding("escape,f", "cancel", "close")]

    _STYLE = {"A": "green", "M": "yellow", "D": "red", "R": "cyan", "•": "dim"}

    def __init__(self, changes: list[tuple[str, str]], on_pick=None):
        super().__init__()
        self._changes = changes
        self._on_pick = on_pick

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        from textual.widgets import ListView
        with Vertical(id="box"):
            yield Label(f"Changed files ({len(self._changes)}) — enter opens in your editor")
            yield ListView(id="picker_list")

    def on_mount(self) -> None:
        from textual.widgets import ListItem, ListView
        lv = self.query_one("#picker_list", ListView)
        for status, path in self._changes:
            color = self._STYLE.get(status, "dim")
            item = ListItem(Label(f"[{color}]{status}[/{color}]  {path}"))
            item.data = path
            lv.append(item)
        if len(self._changes):
            lv.index = 0   # highlight the first row so enter works immediately
        lv.focus()

    def on_list_view_selected(self, event) -> None:
        path = getattr(event.item, "data", None)
        self.app.pop_screen()
        if path and self._on_pick:
            self._on_pick(path)

    def action_cancel(self) -> None:
        self.app.pop_screen()


class TemplatePickerModal(ModalScreen):
    """Pick a saved prompt template. Calls on_pick(entry) with a templates.TemplateEntry."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, entries, on_pick=None):
        super().__init__()
        self._entries = entries
        self._on_pick = on_pick

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        from textual.widgets import ListView
        with Vertical(id="box"):
            yield Label("Load a prompt template — enter to load · esc cancel")
            yield ListView(id="picker_list")

    def on_mount(self) -> None:
        from textual.widgets import ListItem, ListView
        lv = self.query_one("#picker_list", ListView)
        for e in self._entries:
            scope = f" · {', '.join(e.workflows)}" if e.workflows else " · all"
            desc = f"\n  [dim]{e.description}[/dim]" if e.description else ""
            item = ListItem(Label(f"{e.name}[dim]{scope}[/dim]{desc}"))
            item.data = e
            lv.append(item)
        if self._entries:
            lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event) -> None:
        entry = getattr(event.item, "data", None)
        self.app.pop_screen()
        if entry is not None and self._on_pick:
            self._on_pick(entry)

    def action_cancel(self) -> None:
        self.app.pop_screen()


def _parse_repos(raw: str) -> tuple[str, ...]:
    return tuple(r.strip() for r in (raw or "").replace(";", ",").split(",") if r.strip())


class SaveTemplateModal(ModalScreen):
    """Name a prompt template, scope it to this workflow, and optionally to a set of repos.
    Calls on_save(name, scoped: bool, repos: tuple[str, ...])."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, workflow: str, repo: str | None = None, on_save=None):
        super().__init__()
        self._workflow = workflow
        self._repo = repo
        self._on_save = on_save

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        from textual.widgets import Switch
        with Vertical(id="box"):
            yield Label("Save prompt template")
            yield Input(placeholder="template name", id="tpl_name")
            with Horizontal(classes="field-row"):
                yield Label(f"Only for {self._workflow}")
                yield Switch(value=True, id="tpl_scoped")
            hint = f"repos (optional, comma-separated) e.g. {self._repo}" if self._repo \
                else "repos (optional, comma-separated owner/name) — blank = any repo"
            yield Input(placeholder=hint, id="tpl_repos")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save", variant="success", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#tpl_name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()                          # Enter in a field saves

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.action_cancel()

    def _submit(self) -> None:
        from textual.widgets import Switch
        name = self.query_one("#tpl_name", Input).value.strip()
        if not name:
            self.query_one("#tpl_name", Input).focus()
            return
        scoped = self.query_one("#tpl_scoped", Switch).value
        repos = _parse_repos(self.query_one("#tpl_repos", Input).value)
        self.app.pop_screen()
        if self._on_save:
            self._on_save(name, scoped, repos)

    def action_cancel(self) -> None:
        self.app.pop_screen()


class NewTemplateModal(ModalScreen):
    """Name a new template, scope it to one workflow, and optionally to a set of repos.
    Calls on_create(name, workflows: tuple[str, ...], repos: tuple[str, ...])."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, workflows: list[str], on_create=None):
        super().__init__()
        self._workflows = workflows
        self._on_create = on_create

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        from textual.widgets import Select
        with Vertical(id="box"):
            yield Label("New prompt template")
            yield Input(placeholder="template name", id="nt_name")
            opts = [("all workflows", "__all__")] + [(w, w) for w in self._workflows]
            yield Select(opts, value="__all__", allow_blank=False, id="nt_scope")
            yield Input(placeholder="repos (optional, comma-separated owner/name) — blank = any",
                        id="nt_repos")
            with Horizontal(classes="modal-buttons"):
                yield Button("Create", variant="success", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#nt_name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()                          # Enter in a field creates

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.action_cancel()

    def _submit(self) -> None:
        from textual.widgets import Select
        name = self.query_one("#nt_name", Input).value.strip()
        if not name:
            self.query_one("#nt_name", Input).focus()
            return
        scope = self.query_one("#nt_scope", Select).value
        workflows = () if scope in ("__all__", Select.BLANK) else (scope,)
        repos = _parse_repos(self.query_one("#nt_repos", Input).value)
        self.app.pop_screen()
        if self._on_create:
            self._on_create(name, workflows, repos)

    def action_cancel(self) -> None:
        self.app.pop_screen()


class ConfirmModal(ModalScreen):
    """Yes/No confirm. Returns via callback: dismiss(result) where result is None
    (cancelled) or a dict {"confirmed": True, "reason": str}."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title: str, message: str, *, want_reason: bool = False,
                 confirm_label: str = "Confirm", on_confirm=None):
        super().__init__()
        self._title = title
        self._message = message
        self._want_reason = want_reason
        self._confirm_label = confirm_label
        self._on_confirm = on_confirm

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        with Vertical(id="box"):
            yield Label(self._title)
            yield Label(self._message, classes="muted")
            if self._want_reason:
                yield Input(placeholder="reason (optional)", id="reason")
            with Horizontal(classes="modal-buttons"):
                yield Button(self._confirm_label, variant="error", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        # Focus the reason input if there is one, else the confirm button.
        target = "#reason" if self._want_reason else "#ok"
        try:
            self.query_one(target).focus()
        except Exception:  # noqa: BLE001
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            reason = self.query_one("#reason", Input).value if self._want_reason else ""
            if self._on_confirm is not None:      # callback mode (Run Detail)
                self.app.pop_screen()
                self._on_confirm(reason)
            else:                                  # push_screen_wait mode (chat) → return value
                self.dismiss(True)
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        if self._on_confirm is not None:
            self.app.pop_screen()
        else:
            self.dismiss(False)


class ApprovalModal(ModalScreen):
    """HITL review gate: show the drafted output (a PR review's comments, or a PR's
    title/body) and let the user Approve, Reject, or Edit it in their editor first.

    Decision flows back via ``on_decision(status, output)``:
      * Approve → ``("COMPLETED", {approved:true, **payload})`` — payload keys match what
        the workflow's downstream JQ reads (pr_review → ``review``; issue_to_pr →
        ``title``/``body``).
      * Reject  → ``("FAILED_WITH_TERMINAL_ERROR", {approved:false})`` — the run fails.
    Editing round-trips the draft through a local temp JSON file (the draft arrived over
    REST, so a local temp is host-independent); Approve re-reads that file if edited.
    """

    BINDINGS = [
        Binding("a", "approve", "approve"),
        Binding("e", "edit", "edit"),
        Binding("x", "reject", "reject"),
        Binding("escape", "defer", "later"),
    ]

    def __init__(self, gate_workflow: str, draft: dict, *, pr_number=None,
                 issue_number=None, on_decision=None):
        super().__init__()
        self._workflow = gate_workflow
        self._draft = dict(draft or {})
        self._pr_number = pr_number
        self._issue_number = issue_number
        self._on_decision = on_decision
        self._edited_path: str | None = None

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self._heading())
            with VerticalScroll(id="approval_body"):
                yield Static(self._draft_text(), id="approval_content")
            yield Static("edit then Approve to post the edited version", classes="muted",
                        id="approval_hint")
            yield Static("", id="approval_error", classes="banner-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Approve ✓", variant="success", id="approve")
                yield Button("Edit ✎", id="edit")
                yield Button("Reject ✗", variant="error", id="reject")
                yield Button("Later", id="defer")

    def on_mount(self) -> None:
        self.query_one("#approval_error", Static).display = False
        self.query_one("#approve", Button).focus()

    def _heading(self) -> str:
        if self._workflow == "issue_to_pr":
            tgt = f" for issue #{self._issue_number}" if self._issue_number else ""
            return f"Review the pull request{tgt} before it opens"
        tgt = f" on PR #{self._pr_number}" if self._pr_number else ""
        return f"Review the drafted comments{tgt} before they post"

    def _draft_text(self) -> Text:
        d = self._draft
        t = Text()
        if self._workflow == "issue_to_pr":
            t.append("Title: ", style="bold"); t.append(f"{d.get('title', '')}\n")
            if d.get("base") or d.get("head"):
                t.append("Branch: ", style="bold")
                t.append(f"{d.get('head', '?')} → {d.get('base', '?')}\n", style="grey62")
            t.append("\nBody:\n", style="bold")
            t.append(str(d.get("body", "")).strip() + "\n")
            return t
        # pr_review structured review: summary / verdict / inline comments
        t.append("Verdict: ", style="bold")
        t.append(f"{d.get('verdict', '?')}\n", style="yellow")
        t.append("\nSummary:\n", style="bold")
        t.append(str(d.get("summary", "")).strip() + "\n")
        comments = d.get("comments") or []
        t.append(f"\nInline comments ({len(comments)}):\n", style="bold")
        if not comments:
            t.append("  (none)\n", style="grey62")
        for c in comments:
            if not isinstance(c, dict):
                continue
            sev = c.get("severity")
            head = f"  {c.get('path', '?')}:{c.get('line', '?')}"
            t.append(head, style="cyan")
            if sev:
                t.append(f"  [{sev}]", style="grey62")
            t.append("\n")
            t.append(f"    {str(c.get('body', '')).strip()}\n")
        return t

    def _error(self, msg: str) -> None:
        e = self.query_one("#approval_error", Static)
        e.update(msg)
        e.display = bool(msg)

    # ------------------------------------------------------------------ actions
    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "approve":
            self.action_approve()
        elif bid == "edit":
            self.action_edit()
        elif bid == "reject":
            self.action_reject()
        else:
            self.action_defer()

    def _final_draft(self) -> dict | None:
        """The draft to act on: the edited temp file if present (re-read + parsed), else
        the original. Returns None (and shows an error) on unparseable edited JSON."""
        if not self._edited_path:
            return self._draft
        try:
            with open(self._edited_path, encoding="utf-8") as fh:
                parsed = json.load(fh)
        except (OSError, ValueError) as e:
            self._error(f"edited draft isn't valid JSON — fix or Reject: {e}")
            return None
        if not isinstance(parsed, dict):
            self._error("edited draft must be a JSON object")
            return None
        return parsed

    def action_approve(self) -> None:
        draft = self._final_draft()
        if draft is None:
            return
        if self._workflow == "issue_to_pr":
            output = {"approved": True, "title": draft.get("title", ""),
                      "body": draft.get("body", "")}
        else:
            output = {"approved": True, "review": draft}
        self._decide("COMPLETED", output)

    def action_reject(self) -> None:
        self._decide("FAILED_WITH_TERMINAL_ERROR", {"approved": False})

    def action_edit(self) -> None:
        from .. import edit
        if not self._edited_path:
            fd, path = tempfile.mkstemp(prefix="harness_review_", suffix=".json")
            os.close(fd)
            self._edited_path = path
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._draft, fh, indent=2)
        status = edit.open_path(self.app, self._edited_path, self.app.settings.editor)
        self.notify(f"{status} — edit, save, then Approve")

    def action_defer(self) -> None:
        self._decide(None, None)

    def _decide(self, status, output) -> None:
        if self._edited_path:
            try:
                os.unlink(self._edited_path)
            except OSError:
                pass
        self.app.pop_screen()
        if self._on_decision is not None:
            self._on_decision(status, output)
