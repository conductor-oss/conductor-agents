"""Schedule management for the three GitHub automation sweeps."""

from __future__ import annotations

import re
from datetime import datetime

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Select, Static, TextArea

from ..api import ConductorError, Schedule
from ..catalog import short_repo
from ..widgets.factory_bar import FactoryTopBar
from ..widgets.modals import ConfirmModal


AUTOMATIONS = ("pr_review_sweep", "pr_address_sweep", "issue_resolution_sweep")
DEFAULT_CRON = "0 */10 * ? * *"
AUTOMATION_TEMPLATE_FIELDS = {
    "pr_review_sweep": "reviewPromptTemplate",
    "pr_address_sweep": "fixPromptTemplate",
    "issue_resolution_sweep": "codePromptTemplate",
}


def validate_cron(value: str) -> bool:
    return value.startswith("@") or len(value.split()) in (6, 7)


def local_zone() -> str:
    zone = datetime.now().astimezone().tzinfo
    return str(getattr(zone, "key", None) or zone or "UTC")


def schedule_name(workflow: str, repo: str) -> str:
    automation = workflow.removesuffix("_sweep").replace("_", "-")
    slug = short_repo(repo).replace("/", "-")
    return re.sub(r"[^A-Za-z0-9_-]", "-", f"conductor-{automation}-{slug}")


def build_schedule(workflow: str, repo: str, *, cron: str = DEFAULT_CRON,
                   zone_id: str | None = None, approval_mode: str = "human",
                   paused: bool = False, name: str = "",
                   workflow_input: dict | None = None,
                   prompt_template: str = "",
                   prompt_template_source: str = "") -> dict:
    if workflow not in AUTOMATIONS:
        raise ValueError("unknown automation workflow")
    if not validate_cron(cron):
        raise ValueError("Quartz cron must have 6 or 7 fields")
    inputs = dict(workflow_input or {})
    inputs.update({"repo": repo, "approvalMode": approval_mode})
    template_field = AUTOMATION_TEMPLATE_FIELDS[workflow]
    if prompt_template:
        inputs[template_field] = prompt_template
        inputs[f"{template_field}Source"] = (
            prompt_template_source
            or ("schedule:repo-reference" if prompt_template.startswith("@") else "schedule:inline")
        )
    else:
        inputs.pop(template_field, None)
        inputs.pop(f"{template_field}Source", None)
    return {
        "name": name or schedule_name(workflow, repo),
        "description": f"Conductor GitHub automation: {workflow} for {short_repo(repo)}",
        "cronExpression": cron,
        "zoneId": zone_id or local_zone(),
        "paused": paused,
        "runCatchupScheduleInstances": False,
        "startWorkflowRequest": {
            "name": workflow, "version": 1,
            "input": inputs,
            "correlationId": "", "taskToDomain": {}, "priority": 0,
        },
    }


class ScheduleModal(ModalScreen):
    def __init__(self, existing: Schedule | None = None):
        super().__init__()
        self._existing = existing
        self._existing_workflow = existing.workflow if existing else ""
        existing_field = AUTOMATION_TEMPLATE_FIELDS.get(self._existing_workflow, "")
        self._existing_template = str(existing.input.get(existing_field) or "") if existing else ""
        self._existing_template_source = (
            str(existing.input.get(f"{existing_field}Source") or "") if existing else "")

    def compose(self) -> ComposeResult:
        raw = self._existing.raw if self._existing else {}
        request = raw.get("startWorkflowRequest") or {}
        inputs = request.get("input") or {}
        workflow = self._existing.workflow if self._existing else AUTOMATIONS[0]
        template_field = AUTOMATION_TEMPLATE_FIELDS[workflow]
        with Vertical(id="box"):
            yield Label("Edit automation" if self._existing else "New GitHub automation")
            yield Select([(x, x) for x in AUTOMATIONS], value=self._existing.workflow if self._existing else AUTOMATIONS[0], allow_blank=False, id="auto_workflow")
            yield Input(value=str(inputs.get("repo") or ""), placeholder="owner/repo", id="auto_repo")
            yield Select([("Human approval", "human"), ("LLM approval", "llm")], value=str(inputs.get("approvalMode") or "human"), allow_blank=False, id="auto_approval")
            yield Input(value=str(inputs.get("modelProfile") or ""), placeholder="model profile (blank = default)", id="auto_model_profile")
            yield Input(value=str(inputs.get("model") or ""), placeholder="specific model (optional)", id="auto_model")
            yield Label("Primary prompt template (inline text or @repo/path)")
            yield TextArea(str(inputs.get(template_field) or ""), id="auto_template")
            yield Input(value=str(raw.get("cronExpression") or DEFAULT_CRON), id="auto_cron")
            yield Input(value=str(raw.get("zoneId") or local_zone()), id="auto_zone")
            yield Static("Credentials remain in the TUI/worker environment; schedules store only repository and non-secret configuration.", classes="muted")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save", variant="success", id="save")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        workflow = str(self.query_one("#auto_workflow", Select).value)
        repo = self.query_one("#auto_repo", Input).value.strip()
        cron = self.query_one("#auto_cron", Input).value.strip()
        if not repo or not validate_cron(cron):
            self.notify("Repository and a valid 6/7-field Quartz cron are required", severity="error")
            return
        prompt_template = self.query_one("#auto_template", TextArea).text.strip()
        preserved_source = self._existing_template_source if (
            workflow == self._existing_workflow and prompt_template == self._existing_template
        ) else ""
        payload = build_schedule(
            workflow, repo, cron=cron,
            zone_id=self.query_one("#auto_zone", Input).value.strip(),
            approval_mode=str(self.query_one("#auto_approval", Select).value),
            paused=self._existing.paused if self._existing else False,
            name=self._existing.name if self._existing else "",
            workflow_input={**(self._existing.input if self._existing else {}),
                            "modelProfile": self.query_one("#auto_model_profile", Input).value.strip(),
                            "model": self.query_one("#auto_model", Input).value.strip()},
            prompt_template=prompt_template,
            prompt_template_source=preserved_source,
        )
        from .. import templates
        try:
            schedule_input, _applied = templates.apply_user_templates(
                workflow, payload["startWorkflowRequest"]["input"])
        except templates.TemplateSelectionError as exc:
            self.notify(str(exc), severity="error")
            return
        payload["startWorkflowRequest"]["input"] = schedule_input
        self.dismiss(payload)


class AutomationsScreen(Screen):
    BINDINGS = [Binding("n", "new", "new"), Binding("e", "edit", "edit"),
                Binding("p", "toggle_pause", "pause/resume"), Binding("x", "delete", "delete"),
                Binding("r", "run_now", "run now"), Binding("escape", "back", "back")]

    def __init__(self):
        super().__init__()
        self._items: list[Schedule] = []

    def compose(self) -> ComposeResult:
        yield FactoryTopBar()
        yield Static("Automations — scheduled GitHub work loops", id="launcher_title")
        yield DataTable(id="schedule_table", cursor_type="row", zebra_stripes=True)
        yield Static("n new · e edit · p pause/resume · r run now · x delete", id="dash_hint")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#schedule_table", DataTable).add_columns("", "name", "workflow", "repository", "cron", "timezone")
        self.refresh_data()

    @work(exclusive=True, group="schedules")
    async def refresh_data(self) -> None:
        try:
            all_items = await self.app.client.list_schedules()
        except ConductorError:
            return
        self._items = [item for item in all_items if item.workflow in AUTOMATIONS]
        table = self.query_one("#schedule_table", DataTable)
        table.clear()
        for item in self._items:
            table.add_row("⏸" if item.paused else "▶", item.name, item.workflow,
                          str(item.input.get("repo") or ""), item.cron, item.zone_id)

    def _selected(self) -> Schedule | None:
        table = self.query_one("#schedule_table", DataTable)
        return self._items[table.cursor_row] if 0 <= table.cursor_row < len(self._items) else None

    async def _edit_modal(self, existing=None) -> None:
        payload = await self.app.push_screen_wait(ScheduleModal(existing))
        if payload:
            await self.app.client.save_schedule(payload)
            self.refresh_data()

    def action_new(self) -> None:
        self.run_worker(self._edit_modal(), exclusive=True, group="schedule-edit")

    def action_edit(self) -> None:
        if self._selected():
            self.run_worker(self._edit_modal(self._selected()), exclusive=True, group="schedule-edit")

    @work(exclusive=True, group="schedule-mutate")
    async def action_toggle_pause(self) -> None:
        item = self._selected()
        if item:
            await self.app.client.pause_schedule(item.name, not item.paused)
            self.refresh_data()

    @work(exclusive=True, group="schedule-mutate")
    async def action_run_now(self) -> None:
        item = self._selected()
        if item:
            workflow_id = await self.app.client.run_schedule_now(item)
            self.app.track(workflow_id)
            self.notify(f"started {workflow_id}")

    async def _delete(self, item: Schedule) -> None:
        confirmed = await self.app.push_screen_wait(ConfirmModal("Delete schedule", f"Delete {item.name}?", confirm_label="Delete"))
        if confirmed:
            await self.app.client.delete_schedule(item.name)
            self.refresh_data()

    def action_delete(self) -> None:
        if self._selected():
            self.run_worker(self._delete(self._selected()), exclusive=True, group="schedule-delete")

    def action_back(self) -> None:
        self.app.pop_screen()
