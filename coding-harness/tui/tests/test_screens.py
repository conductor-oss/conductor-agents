"""Screen pilot tests — network-free via a FakeClient injected into the app."""

from __future__ import annotations

import json
import pathlib

import pytest

from tui import api
from tui.app import HarnessApp
from tui.config import Settings

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"


def _fix(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeClient:
    """Stands in for ConductorClient; returns canned data, records mutations."""

    def __init__(self, runs=None, execution=None, workers_alive=True):
        self._runs = runs or []
        self._execution = execution
        self._workers_alive = workers_alive
        self.started = []
        self.terminated = []
        self.signals = []

    async def search_runs(self, limit=50):
        return self._runs

    async def get_run(self, wid, recurse=True, only_running=False):
        run, tasks = api.parse_execution(self._execution)
        return api.RunDetail(run=run, tasks=tasks)

    async def health(self):
        st = api.PollState("coding_agent", self._workers_alive, 2.0, 1)
        st2 = api.PollState("gitops", self._workers_alive, 1.0, 1)
        return {"coding_agent": st, "gitops": st2}

    async def workflow_registered(self, name):
        return True

    async def start(self, name, payload):
        self.started.append((name, payload))
        return "wf-new-123"

    async def terminate(self, wid, reason=""):
        self.terminated.append((wid, reason))

    async def retry(self, wid):
        pass

    async def signal_task(self, wid, task_ref, status, output=None):
        self.signals.append((wid, task_ref, status, output))

    async def task_logs(self, task_id):
        return ["line 1", "line 2"]

    async def aclose(self):
        pass


def _app(client) -> HarnessApp:
    # start on the dashboard for these screen tests (chat is the real default landing)
    return HarnessApp(Settings(server_url="http://x/api", notify=False), client=client,
                      start_dashboard=True)


@pytest.mark.asyncio
async def test_dashboard_lists_runs_and_filters():
    runs = [
        api.Run(id="a" * 8, workflow="pr_review", status="COMPLETED", start_ms=1000, end_ms=2000,
                output={"tokenUsed": 11569, "costUsd": 0.05}, input={"repo": "acme/app", "prNumber": 7}),
        api.Run(id="b" * 8, workflow="code_parallel", status="FAILED", start_ms=1000, end_ms=1500,
                input={"repoPath": "/tmp/x"}),
    ]
    app = _app(FakeClient(runs=runs))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        from textual.widgets import DataTable
        table = app.screen.query_one("#run_table", DataTable)
        assert table.row_count == 2
        # cycle to FAILED
        app.screen._filter = 2
        app.screen._repopulate()
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_launcher_blocks_start_without_required():
    from tui.screens.launcher import LauncherForm
    app = _app(FakeClient())
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.2)
        app.push_screen(LauncherForm("pr_review"))
        await pilot.pause(0.4)
        scr = app.screen
        # leave required repo/prNumber blank → collect reports missing
        values, missing = scr._collect()
        assert "Repo" in missing and "PR" in missing
        # now fill and confirm payload builds
        scr._set("repo", "acme/app")
        scr._widgets["prNumber"].value = "7"
        values, missing = scr._collect()
        assert not missing
        # the review gate defaults ON in the form (tui_default) → approve sent true
        assert scr.spec.build_payload(values) == {
            "repo": "acme/app", "prNumber": 7, "approve": True}


@pytest.mark.asyncio
async def test_launcher_picker_select_flows_to_payload():
    """Picking a template in the visible Select loads it into the field and sends it."""
    from tui import templates
    from tui.screens.launcher import LauncherForm
    from textual.widgets import Select, TextArea
    templates.save("Sec", "Security review only.", workflows=("pr_review",))
    templates.save("Perf", "Perf review only.", workflows=("pr_review",))
    app = _app(FakeClient())
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause(0.2)
        app.push_screen(LauncherForm("pr_review"))
        await pilot.pause(0.4)
        scr = app.screen
        sel = scr.query_one("#tplsel", Select)
        assert sel.value == "__builtin__"                       # >1 → user picks, none auto
        assert scr._widgets["reviewPromptTemplate"].text == ""
        perf = next(e for e in templates.list_templates("pr_review") if e.name == "Perf")
        sel.value = str(perf.path)                              # pick one → drives the TextArea
        await pilot.pause(0.2)
        assert scr._widgets["reviewPromptTemplate"].text == "Perf review only."
        scr._set("repo", "acme/app")
        scr._widgets["prNumber"].value = "7"
        values, missing = scr._collect()
        assert not missing
        assert scr.spec.build_payload(values)["reviewPromptTemplate"] == "Perf review only."


@pytest.mark.asyncio
async def test_templates_screen_lists_edits_creates_deletes(monkeypatch):
    from tui import edit, templates
    from tui.screens.templates import TemplatesScreen
    templates.save("Alpha review", "focus on X", workflows=("pr_review",))
    templates.save("Beta", "y")
    opened: list[str] = []
    monkeypatch.setattr(edit, "open_path", lambda app, path, override=None: opened.append(path) or "opened")
    app = _app(FakeClient())
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.2)
        app.push_screen(TemplatesScreen())
        await pilot.pause(0.3)
        scr = app.screen
        names = {e.name for e in scr._entries}
        assert {"Alpha review", "Beta"} <= names
        # edit the first → external editor opens that file
        scr.action_edit()
        assert opened and opened[-1].endswith(".md")
        # create a new one → file written + opened for editing
        scr._create("Gamma", ("code_parallel",))
        assert any(e.name == "Gamma" for e in scr._entries)
        assert opened[-1].endswith("gamma.md")
        # delete it → gone after reload
        entry = next(e for e in scr._entries if e.name == "Gamma")
        scr._do_delete(entry)
        assert not any(e.name == "Gamma" for e in scr._entries)
        assert not entry.path.exists()


@pytest.mark.asyncio
async def test_templates_new_modal_creates_via_enter_and_button(monkeypatch):
    """The New-template modal must create a file both on Enter in the name field and on the
    Create button (regression: the button/Enter path was previously unwired in real terminals)."""
    from tui import edit, templates
    from tui.screens.templates import TemplatesScreen
    from tui.widgets.modals import NewTemplateModal
    from textual.widgets import Input
    monkeypatch.setattr(edit, "open_path", lambda app, path, override=None: "opened")
    app = _app(FakeClient())
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.2)
        app.push_screen(TemplatesScreen())
        await pilot.pause(0.3)
        # 1) Enter in the name field creates
        await pilot.press("n")
        await pilot.pause(0.2)
        assert isinstance(app.screen, NewTemplateModal)
        app.screen.query_one("#nt_name", Input).value = "Via Enter"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, NewTemplateModal)          # modal closed
        assert any(e.name == "Via Enter" for e in templates.list_templates())
        # 2) the Create button creates
        await pilot.press("n")
        await pilot.pause(0.2)
        app.screen.query_one("#nt_name", Input).value = "Via Button"
        await pilot.click("#ok")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, NewTemplateModal)
        assert any(e.name == "Via Button" for e in templates.list_templates())
        # empty name is a no-op (modal stays open)
        await pilot.press("n")
        await pilot.pause(0.2)
        await pilot.click("#ok")
        await pilot.pause(0.2)
        assert isinstance(app.screen, NewTemplateModal)


@pytest.mark.asyncio
async def test_launcher_auto_selects_single_template():
    """Exactly one applicable template → the picker auto-selects it and it's used, no clicks."""
    from tui import templates
    from tui.screens.launcher import LauncherForm
    from textual.widgets import Select, TextArea
    templates.save("My review", "Review for security only.", workflows=("pr_review",))
    app = _app(FakeClient())
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause(0.2)
        app.push_screen(LauncherForm("pr_review"))
        await pilot.pause(0.4)
        scr = app.screen
        w = scr._widgets["reviewPromptTemplate"]
        entry = templates.list_templates("pr_review")[0]
        sel = scr.query_one("#tplsel", Select)
        assert sel.value == str(entry.path)                     # auto-selected
        assert isinstance(w, TextArea) and w.text == "Review for security only."
        scr._set("repo", "acme/app")
        scr._widgets["prNumber"].value = "7"
        values, missing = scr._collect()
        assert not missing
        assert scr.spec.build_payload(values)["reviewPromptTemplate"] == "Review for security only."


@pytest.mark.asyncio
async def test_launcher_picker_repo_filter_rebuilds():
    """Repo-scoped templates only appear for a matching target repo; the picker re-filters when
    the repo field changes."""
    from tui import templates
    from tui.screens.launcher import LauncherForm
    from textual.widgets import Select
    templates.save("General", "GEN", workflows=("pr_review",))
    templates.save("SDK only", "SDK", workflows=("pr_review",), repos=("conductor-oss/python-sdk",))
    app = _app(FakeClient())
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause(0.2)
        app.push_screen(LauncherForm("pr_review"))
        await pilot.pause(0.4)
        scr = app.screen
        sel = scr.query_one("#tplsel", Select)
        # no repo yet → only the unrestricted "General" applies → auto-selected
        assert scr._widgets["reviewPromptTemplate"].text == "GEN"
        # target the SDK repo → the repo-scoped template becomes applicable (now 2 → keep General)
        scr._set("repo", "https://github.com/conductor-oss/python-sdk.git")
        await pilot.pause(0.3)
        opt_values = [str(e.path) for e in
                      templates.list_templates("pr_review", repo="conductor-oss/python-sdk")]
        assert len(opt_values) == 2                             # General + SDK only now
        sdk = next(e for e in templates.list_templates("pr_review", repo="conductor-oss/python-sdk")
                   if e.name == "SDK only")
        sel.value = str(sdk.path)
        await pilot.pause(0.2)
        assert scr._widgets["reviewPromptTemplate"].text == "SDK"
        # a different repo → SDK-only drops out
        assert len(templates.list_templates("pr_review", repo="acme/other")) == 1


@pytest.mark.asyncio
async def test_run_detail_completed_shows_result_card():
    app = _app(FakeClient(execution=_fix("pr_review_completed")))
    async with app.run_test(size=(140, 45)) as pilot:
        from tui.screens.run_detail import RunDetail
        from tui.widgets.result_card import ResultCard
        await pilot.pause(0.2)
        app.push_screen(RunDetail("x"))
        await pilot.pause(0.6)
        scr = app.screen
        assert scr.detail is not None and not scr.detail.run.running
        assert scr.query_one(ResultCard).display is True
        assert scr._primary_url and "/pull/" in scr._primary_url


@pytest.mark.asyncio
async def test_run_detail_rerun_opens_prefilled_form():
    """'run again' (n) must open a LauncherForm prefilled from the run's input, not crash."""
    app = _app(FakeClient(execution=_fix("pr_review_completed")))
    async with app.run_test(size=(140, 45)) as pilot:
        from tui.screens.run_detail import RunDetail
        from tui.screens.launcher import LauncherForm
        await pilot.pause(0.2)
        app.push_screen(RunDetail("x"))
        await pilot.pause(0.6)
        await pilot.press("n")             # action_rerun
        await pilot.pause(0.4)
        assert isinstance(app.screen, LauncherForm)
        assert app.screen.spec.name == "pr_review"
        # prefilled from the completed run's input
        assert app.screen._value("repo") and app.screen._value("prNumber") == "4"


@pytest.mark.asyncio
async def test_run_detail_recurses_subworkflows():
    app = _app(FakeClient(execution=_fix("issue_to_pr_running")))
    async with app.run_test(size=(140, 45)) as pilot:
        from tui.screens.run_detail import RunDetail
        await pilot.pause(0.2)
        app.push_screen(RunDetail("x"))
        await pilot.pause(0.6)
        d = app.screen.detail
        assert any(t.type == "SUB_WORKFLOW" for t in d.tasks)


# --------------------------------------------------------------------------- HITL gate

def _pr_review_gate_execution() -> dict:
    """A pr_review run paused at its review_gate HUMAN task, draft in inputData."""
    return {
        "workflowId": "wf-gate",
        "workflowType": "pr_review",
        "status": "RUNNING",
        "startTime": 1000,
        "input": {"repo": "acme/app", "prNumber": 7, "approve": True},
        "tasks": [
            {"referenceTaskName": "review", "taskDefName": "coding_agent", "taskType": "SIMPLE",
             "status": "COMPLETED", "taskId": "t1",
             "outputData": {"structured": {"summary": "ok", "verdict": "comment", "comments": []}}},
            {"referenceTaskName": "review_gate", "taskType": "HUMAN", "status": "IN_PROGRESS",
             "taskId": "gate-1",
             "inputData": {"workflow": "pr_review", "prNumber": 7,
                           "draft": {"summary": "Looks fine overall", "verdict": "comment",
                                     "comments": [{"path": "a.py", "line": 3, "body": "nit: rename"}]}}},
        ],
    }


def _design_gate_execution() -> dict:
    return {"workflowId": "wf-design-gate", "workflowType": "design_docs", "status": "RUNNING", "startTime": 1000,
            "input": {"repoPath": "/tmp/app", "instruction": "Design the change"},
            "tasks": [{"referenceTaskName": "design", "taskDefName": "coding_agent", "taskType": "SIMPLE", "status": "COMPLETED", "taskId": "d1", "outputData": {"filesChanged": ["docs/design/architecture.md"]}},
                      {"referenceTaskName": "design_review", "taskType": "HUMAN", "status": "IN_PROGRESS", "taskId": "design-review-1", "inputData": {"workflow": "design_docs", "draft": {"designDir": "docs/design", "filesChanged": ["docs/design/architecture.md"], "summary": "Initial design"}}}]}


def test_pending_gate_detection():
    run, tasks = api.parse_execution(_pr_review_gate_execution())
    d = api.RunDetail(run=run, tasks=tasks)
    gate = d.pending_gate()
    assert gate is not None and gate.ref == "review_gate" and gate.type == "HUMAN"
    assert gate.input["draft"]["verdict"] == "comment"


@pytest.mark.asyncio
async def test_run_detail_gate_auto_opens_and_approves():
    fc = FakeClient(execution=_pr_review_gate_execution())
    app = _app(fc)
    async with app.run_test(size=(140, 45)) as pilot:
        from tui.screens.run_detail import RunDetail
        from tui.widgets.modals import ApprovalModal
        await pilot.pause(0.2)
        app.push_screen(RunDetail("wf-gate"))
        await pilot.pause(0.6)
        assert isinstance(app.screen, ApprovalModal)   # auto-opened on pause
        await pilot.click("#approve")
        await pilot.pause(0.5)
        assert fc.signals, "expected a signal_task call"
        wid, ref, status, output = fc.signals[-1]
        assert wid == "wf-gate" and ref == "review_gate" and status == "COMPLETED"
        assert output["approved"] is True and output["review"]["verdict"] == "comment"


@pytest.mark.asyncio
async def test_run_detail_gate_reject_fails_run():
    fc = FakeClient(execution=_pr_review_gate_execution())
    app = _app(fc)
    async with app.run_test(size=(140, 45)) as pilot:
        from tui.screens.run_detail import RunDetail
        from tui.widgets.modals import ApprovalModal
        await pilot.pause(0.2)
        app.push_screen(RunDetail("wf-gate"))
        await pilot.pause(0.6)
        assert isinstance(app.screen, ApprovalModal)
        await pilot.click("#reject")
        await pilot.pause(0.5)
        _, ref, status, output = fc.signals[-1]
        assert ref == "review_gate" and status == "FAILED_WITH_TERMINAL_ERROR"
        assert output["approved"] is False


@pytest.mark.asyncio
async def test_design_gate_requests_changes_with_feedback_and_keeps_loop_alive():
    fc = FakeClient(execution=_design_gate_execution())
    app = _app(fc)
    async with app.run_test(size=(140, 45)) as pilot:
        from textual.widgets import TextArea
        from tui.screens.run_detail import RunDetail
        from tui.widgets.modals import ApprovalModal
        await pilot.pause(0.2); app.push_screen(RunDetail("wf-design-gate")); await pilot.pause(0.6)
        assert isinstance(app.screen, ApprovalModal)
        app.screen.query_one("#design_feedback", TextArea).text = "Specify rollback semantics."
        await pilot.click("#reject"); await pilot.pause(0.5)
        _, ref, status, output = fc.signals[-1]
        assert ref == "design_review" and status == "COMPLETED"
        assert output == {"approved": False, "feedback": "Specify rollback semantics."}


@pytest.mark.asyncio
async def test_run_detail_gate_defer_leaves_paused():
    fc = FakeClient(execution=_pr_review_gate_execution())
    app = _app(fc)
    async with app.run_test(size=(140, 45)) as pilot:
        from tui.screens.run_detail import RunDetail
        from tui.widgets.modals import ApprovalModal
        await pilot.pause(0.2)
        app.push_screen(RunDetail("wf-gate"))
        await pilot.pause(0.6)
        assert isinstance(app.screen, ApprovalModal)
        await pilot.press("escape")            # defer
        await pilot.pause(0.5)
        assert not fc.signals                  # nothing submitted; run stays paused
        assert not isinstance(app.screen, ApprovalModal)   # modal closed
        # doesn't auto-reopen on the next poll (already prompted this gate)
        await pilot.pause(2.2)
        assert not isinstance(app.screen, ApprovalModal)
