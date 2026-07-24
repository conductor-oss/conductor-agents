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
from textual.widgets import Button, Input, Label, RichLog, Static, Switch, TextArea


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


class FilePreviewModal(ModalScreen):
    """Read a local changed file without leaving a review checkpoint."""

    BINDINGS = [Binding("escape,q", "dismiss", "close"), Binding("e", "open_editor", "open editor")]
    _MAX_BYTES = 256 * 1024

    def __init__(self, path: str, label: str, *, on_open=None):
        super().__init__()
        self._path = path
        self._label = label
        self._on_open = on_open

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"Design file — {self._label}")
            with VerticalScroll(id="file_preview_scroll"):
                yield Static("", id="file_preview", markup=False)
            with Horizontal(classes="modal-buttons"):
                yield Button("Open in editor", id="open_editor")
                yield Button("Close", variant="primary", id="close")

    def on_mount(self) -> None:
        try:
            with open(self._path, encoding="utf-8", errors="replace") as handle:
                content = handle.read(self._MAX_BYTES + 1)
            if len(content.encode("utf-8")) > self._MAX_BYTES:
                content = content[:self._MAX_BYTES] + "\n\n… preview truncated at 256 KiB; open in editor for the full file."
        except OSError as exc:
            content = f"Could not read this file: {exc}"
        self.query_one("#file_preview", Static).update(Text(content))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open_editor":
            self.action_open_editor()
        else:
            self.action_dismiss()

    def action_open_editor(self) -> None:
        if self._on_open is not None:
            self._on_open(self._path)

    def action_dismiss(self) -> None:
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
        Binding("r", "revise", "revise"),
        Binding("s", "stop", "stop"),
        Binding("f", "design_files", "view files"),
        Binding("e", "edit", "edit"),
        Binding("x", "reject", "reject"),
        Binding("escape", "defer", "later"),
    ]

    def __init__(self, gate_workflow: str, draft: dict, *, pr_number=None,
                 issue_number=None, workspace_path: str | None = None, on_decision=None):
        super().__init__()
        self._workflow = gate_workflow
        self._draft = dict(draft or {})
        self._pr_number = pr_number
        self._issue_number = issue_number
        self._workspace_path = workspace_path
        self._on_decision = on_decision
        self._edited_path: str | None = None

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self._heading())
            with VerticalScroll(id="approval_body"):
                yield Static(self._draft_text(), id="approval_content")
                if self._workflow == "feature_campaign":
                    yield Label("Feedback / reconciliation instruction", classes="muted")
                    yield TextArea("", id="campaign_feedback")
                    yield Input(placeholder="check profile (for run checks)", id="campaign_profile")
                    yield Input(placeholder="specific check IDs, comma-separated (optional)", id="campaign_checks")
                    yield Input(placeholder="wave profile (for set profiles)", id="campaign_wave_profile")
                    yield Input(placeholder="final profile (for set profiles)", id="campaign_final_profile")
                    yield Input(placeholder="raise max turns for resumed agents (optional)", id="campaign_max_turns")
                    yield Input(placeholder="raise max budget USD per resumed agent (optional)", id="campaign_max_budget")
                    with Horizontal(classes="field-row"):
                        yield Label("Attached server confirmed")
                        yield Switch(value=False, id="campaign_attached")
                elif self._workflow == "openspec_plan":
                    yield Label("Feedback for the next OpenSpec plan pass", classes="muted")
                    yield TextArea("", id="plan_feedback")
                elif self._workflow in ("pr_review", "address_pr", "issue_to_pr"):
                    yield Label("Revision feedback (required for Revise)", classes="muted")
                    yield TextArea("", id="approval_feedback")
            hint = ("Choose a phase-aware action; Later leaves this checkpoint open indefinitely"
                    if self._workflow == "feature_campaign"
                    else "View files (f), then approve the plan or request another pass"
                    if self._workflow == "openspec_plan"
                    else "edit then Approve to post the edited version")
            yield Static(hint, classes="muted", id="approval_hint")
            yield Static("", id="approval_error", classes="banner-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Continue ✓" if self._workflow == "feature_campaign" else "Approve ✓",
                             variant="success", id="approve")
                if self._workflow == "feature_campaign":
                    yield Button("Revise ↻", variant="warning", id="campaign_revise")
                    yield Button("Adopt edits", id="campaign_adopt")
                    yield Button("Run checks", id="campaign_run_checks")
                    yield Button("Set profiles", id="campaign_set_profiles")
                    yield Button("Stop", variant="error", id="campaign_stop")
                elif self._workflow != "openspec_plan":
                    yield Button("Edit ✎", id="edit")
                elif self._workflow == "openspec_plan":
                    yield Button("View files", id="design_files")
                if self._workflow != "feature_campaign":
                    reject_label = "Request changes ↻" if self._workflow == "openspec_plan" else "Revise ↻"
                    yield Button(reject_label, variant="warning", id="reject")
                    if self._workflow != "openspec_plan":
                        yield Button("Stop", variant="error", id="stop")
                yield Button("Later", id="defer")

    def on_mount(self) -> None:
        self.query_one("#approval_error", Static).display = False
        if self._workflow == "feature_campaign":
            self.query_one("#approve", Button).focus()
        elif self._workflow == "openspec_plan":
            self.query_one("#plan_feedback", TextArea).focus()
        else:
            self.query_one("#approve", Button).focus()

    def _heading(self) -> str:
        if self._workflow == "feature_campaign":
            phase = str(self._draft.get("phase") or "checkpoint").replace("_", " ")
            return f"Feature campaign — {phase} checkpoint"
        if self._workflow == "openspec_plan":
            return "Review the OpenSpec plan before coding starts"
        if self._workflow == "issue_to_pr":
            tgt = f" for issue #{self._issue_number}" if self._issue_number else ""
            return f"Review the pull request{tgt} before it opens"
        tgt = f" on PR #{self._pr_number}" if self._pr_number else ""
        return f"Review the drafted comments{tgt} before they post"

    def _draft_text(self) -> Text:
        d = self._draft
        t = Text()
        if self._workflow == "feature_campaign":
            for label, key in (("Phase", "phase"), ("Wave", "wave"), ("Branch", "branch"),
                               ("Status", "status"), ("Session", "sessionId")):
                if d.get(key) not in (None, ""):
                    t.append(f"{label}: ", style="bold")
                    t.append(str(d[key]) + "\n")
            for label, key in (("Files", "filesChanged"), ("Ready tasks", "readyTasks"),
                               ("Remaining", "remainingTasks"), ("Errors", "errors"),
                               ("Checks", "checks"), ("Integration", "integration"),
                               ("Review", "review"), ("Summary", "summary"), ("Plan", "plan")):
                value = d.get(key)
                if value not in (None, "", [], {}):
                    t.append(f"\n{label}:\n", style="bold")
                    t.append(json.dumps(value, indent=2, default=str) if isinstance(value, (dict, list)) else str(value))
                    t.append("\n")
            return t
        if self._workflow == "openspec_plan":
            t.append("Change directory: ", style="bold")
            t.append(str(d.get("changeDir", "")) + "\n")
            files = d.get("filesChanged") or []
            t.append("Files changed: ", style="bold")
            t.append(", ".join(str(x) for x in files) if isinstance(files, list) else str(files))
            t.append("\n\nAgent summary:\n", style="bold")
            t.append(str(d.get("summary", "")).strip() + "\n")
            return t
        if self._workflow == "issue_to_pr":
            t.append("Title: ", style="bold"); t.append(f"{d.get('title', '')}\n")
            if d.get("base") or d.get("head"):
                t.append("Branch: ", style="bold")
                t.append(f"{d.get('head', '?')} → {d.get('base', '?')}\n", style="grey62")
            t.append("\nBody:\n", style="bold")
            t.append(str(d.get("body", "")).strip() + "\n")
            for label, key in (("Diff", "diff"), ("Checks", "checks")):
                value = d.get(key)
                if value not in (None, "", [], {}):
                    t.append(f"\n{label}:\n", style="bold")
                    t.append(json.dumps(value, indent=2, default=str)
                             if isinstance(value, (dict, list)) else str(value))
                    t.append("\n")
            return t
        if self._workflow == "address_pr":
            for label, key in (("Branch", "branch"), ("Diff", "diff"), ("Checks", "checks"),
                               ("Summary", "summary")):
                value = d.get(key)
                if value not in (None, "", [], {}):
                    t.append(f"{label}:\n", style="bold")
                    t.append(json.dumps(value, indent=2, default=str)
                             if isinstance(value, (dict, list)) else str(value))
                    t.append("\n\n")
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
        elif bid == "stop":
            self.action_stop()
        elif bid == "design_files":
            self.action_design_files()
        elif bid and bid.startswith("campaign_"):
            self._campaign_action(bid.removeprefix("campaign_"))
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
        if self._workflow == "feature_campaign":
            output = self._campaign_output("continue")
        elif self._workflow == "openspec_plan":
            output = {"approved": True, "feedback": ""}
        elif self._workflow == "issue_to_pr":
            output = {"approved": True, "action": "approve", "title": draft.get("title", ""),
                      "body": draft.get("body", "")}
        elif self._workflow == "address_pr":
            output = {"approved": True, "action": "approve", "artifact": draft}
        else:
            output = {"approved": True, "action": "approve", "review": draft}
        self._decide("COMPLETED", output)

    def _campaign_output(self, action: str) -> dict:
        feedback = self.query_one("#campaign_feedback", TextArea).text.strip()
        checks = [x.strip() for x in self.query_one("#campaign_checks", Input).value.split(",") if x.strip()]
        return {
            "action": action,
            "feedback": feedback,
            "profile": self.query_one("#campaign_profile", Input).value.strip(),
            "checks": checks,
            "attachedConfirmed": self.query_one("#campaign_attached", Switch).value,
            "profiles": {
                "wave": self.query_one("#campaign_wave_profile", Input).value.strip(),
                "final": self.query_one("#campaign_final_profile", Input).value.strip(),
            },
            "maxTurns": self._campaign_number("#campaign_max_turns", int),
            "maxBudgetUsd": self._campaign_number("#campaign_max_budget", float),
        }

    def _campaign_number(self, selector: str, cast):
        value = self.query_one(selector, Input).value.strip()
        if not value:
            return None
        try:
            return cast(value)
        except ValueError:
            return value

    def _campaign_action(self, action: str) -> None:
        mapped = {"adopt": "adopt_edits", "run_checks": "run_checks",
                  "set_profiles": "set_profiles", "revise": "revise", "stop": "stop"}.get(action, action)
        output = self._campaign_output(mapped)
        if mapped == "revise" and not output["feedback"]:
            self._error("Add actionable feedback before requesting a revision.")
            return
        if mapped == "run_checks" and not output["profile"]:
            self._error("Choose a check profile before running checks.")
            return
        if mapped == "set_profiles" and not any(output["profiles"].values()):
            self._error("Enter at least one wave or final profile.")
            return
        self._decide("COMPLETED", output)

    def action_reject(self) -> None:
        if self._workflow == "feature_campaign":
            self._campaign_action("stop")
            return
        if self._workflow == "openspec_plan":
            feedback = self.query_one("#plan_feedback", TextArea).text.strip()
            if not feedback:
                self._error("Add actionable feedback before requesting another plan pass.")
                return
            self._decide("COMPLETED", {"approved": False, "feedback": feedback})
            return
        feedback = self.query_one("#approval_feedback", TextArea).text.strip()
        if not feedback:
            self._error("Add actionable feedback before requesting a revision.")
            return
        # v3 review/address workflows route this completed decision into a new
        # execution in the same worktree. Other workflows retain fail-closed behavior.
        status = "COMPLETED" if self._workflow in ("pr_review", "address_pr") \
            else "FAILED_WITH_TERMINAL_ERROR"
        self._decide(status,
                     {"approved": False, "action": "revise", "feedback": feedback})

    def action_revise(self) -> None:
        self.action_reject()

    def action_stop(self) -> None:
        status = "COMPLETED" if self._workflow in ("pr_review", "address_pr") \
            else "FAILED_WITH_TERMINAL_ERROR"
        self._decide(status,
                     {"approved": False, "action": "stop", "suppressed": True, "feedback": ""})

    def action_edit(self) -> None:
        if self._workflow == "feature_campaign":
            self._campaign_action("adopt_edits")
            return
        from .. import edit
        if not self._edited_path:
            fd, path = tempfile.mkstemp(prefix="harness_review_", suffix=".json")
            os.close(fd)
            self._edited_path = path
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._draft, fh, indent=2)
        status = edit.open_path(self.app, self._edited_path, self.app.settings.editor)
        self.notify(f"{status} — edit, save, then Approve")

    def _design_file_path(self, relative_path: str) -> str | None:
        """Resolve a reported file only if it remains inside the run worktree."""
        if not self._workspace_path:
            return None
        root = os.path.realpath(self._workspace_path)
        candidate = os.path.realpath(os.path.join(root, relative_path))
        if candidate == root or not candidate.startswith(root + os.sep):
            return None
        return candidate

    def action_design_files(self) -> None:
        files = self._draft.get("filesChanged") or []
        if not isinstance(files, list) or not files:
            self._error("This design pass did not report any changed files.")
            return
        if not self._workspace_path:
            self._error("The design worktree is not available on this host.")
            return
        available: dict[str, str] = {}
        for value in files:
            rel = str(value).strip()
            full = self._design_file_path(rel)
            if full and os.path.isfile(full):
                available[rel] = full
        if not available:
            self._error("The reported design files are not available in the local worktree.")
            return

        def preview(rel: str) -> None:
            full = available.get(rel)
            if not full:
                self._error(f"{rel} is no longer available in the worktree.")
                return
            from .. import edit
            self.app.push_screen(FilePreviewModal(
                full, rel,
                on_open=lambda path: self.notify(edit.open_path(
                    self.app, path, self.app.settings.editor
                )),
            ))

        self.app.push_screen(FileListModal([("•", rel) for rel in available], on_pick=preview))

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
