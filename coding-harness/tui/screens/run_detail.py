"""Run Detail — watch the agents work: task tree, live turn trace, result card."""

from __future__ import annotations

import webbrowser

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Static, Tree

from .. import catalog, format as fmt, notify
from ..api import ConductorError, RunDetail, TaskNode
from ..widgets.factory_bar import FactoryTopBar
from ..widgets.modals import ApprovalModal, ConfirmModal, LogsModal
from ..widgets.result_card import ResultCard
from ..widgets.task_tree import TaskTree
from ..widgets.turn_log import TurnLog


class RunDetail(Screen):
    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("t", "terminate", "terminate"),
        Binding("r", "retry", "retry"),
        Binding("R", "start_over", "start over"),
        Binding("l", "logs", "logs"),
        Binding("o", "open_url", "open"),
        Binding("a", "review_gate", "review"),
        Binding("e", "open_folder", "editor"),
        Binding("f", "files", "files"),
        Binding("c", "conductor", "conductor UI"),
        Binding("y", "yank", "copy id"),
        Binding("n", "rerun", "run again"),
        Binding("b", "board", "team board"),
        Binding("F", "continue_run", "continue"),
        Binding("P", "pull_request", "pull request"),
        Binding("M", "merge", "merge"),
    ]

    def __init__(self, workflow_id: str):
        super().__init__()
        self._id = workflow_id
        self.detail: RunDetail | None = None
        self._selected_ref: str | None = None
        self._primary_url: str | None = None
        self._timer = None
        self._gate_open = False
        self._gate_prompted: set[str] = set()

    def compose(self) -> ComposeResult:
        yield FactoryTopBar()
        yield Static("", id="detail_header")
        with Horizontal(id="detail_body"):
            yield TaskTree()
            with Vertical(id="agent_pane"):
                yield TurnLog()
                rc = ResultCard()
                rc.display = False
                yield rc
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_detail()
        self._timer = self.set_interval(2.0, self.refresh_detail)

    # ------------------------------------------------------------------ data
    @work(exclusive=True, group="detail")
    async def refresh_detail(self) -> None:
        try:
            detail = await self.app.client.get_run(self._id, recurse=True, only_running=False)
        except ConductorError:
            return
        self.detail = detail
        self._paint()
        if not detail.run.running:
            if self._timer:
                self._timer.stop()
            self._maybe_notify(detail)

    def _paint(self) -> None:
        d = self.detail
        if not d:
            return
        self._render_header(d)
        self.query_one(TaskTree).sync(d.tasks)
        turn_log = self.query_one(TurnLog)
        card = self.query_one(ResultCard)
        if d.run.running:
            turn_log.display = True
            card.display = False
            turn_log.show(self._current_task(d))
        else:
            turn_log.display = False
            card.display = True
            self._primary_url = card.show(d)
        self._check_gate(d)

    def _render_header(self, d: RunDetail) -> None:
        run = d.run
        tokens, cost = d.tokens_cost()
        t = Text()
        t.append(f"{run.workflow} ", style="bold")
        t.append(f"{fmt.short_id(run.id)} · ", style="grey62")
        t.append(f"{run.target} · ", style="grey70")
        t.append(f"{fmt.status_glyph(run.status)} {run.status}", style=fmt.status_color(run.status))
        t.append(f" · {fmt.duration(run.duration_ms())} · "
                 f"{fmt.tokens(tokens)} tok · {fmt.cost(cost)}", style="grey62")
        # A program's definition of done: the score against its threshold, the ladder rung it ended on
        # and what it needs (PROGRAM_NEEDS is never a failure: Continue resumes at that rung).
        out = run.output or {}
        if out.get("score") is not None:
            try:
                score, thr = float(out.get("score")), float(out.get("threshold") or 1.0)
            except (TypeError, ValueError):
                score, thr = 0.0, 1.0
            t.append(f" · done {score:.2f}/{thr:.2f}", style="green" if score >= thr - 1e-9 else "yellow")
            if out.get("rung"):
                t.append(f" · {out['rung']}", style="grey62")
            needs = out.get("needs") if isinstance(out.get("needs"), list) else []
            if needs:
                t.append(" · needs " + ", ".join(str((n or {}).get("kind") or "") for n in needs if isinstance(n, dict)),
                         style="bold yellow")
        gate = d.pending_gate() if run.running else None
        if gate and gate.input.get("workflow") == "feature_campaign":
            phase = gate.input.get("phase") or "checkpoint"
            wave = gate.input.get("wave")
            draft = gate.input.get("draft") or {}
            profile_data = draft.get("profiles") or {}
            checks = draft.get("checks") or {}
            t.append(f" · phase={phase}", style="cyan")
            if wave not in (None, ""):
                t.append(f" wave={wave}", style="cyan")
            if profile_data:
                t.append(f" profiles={profile_data}", style="grey62")
            if isinstance(checks, dict) and "blockingPassed" in checks:
                t.append(" checks=pass" if checks.get("blockingPassed") else " checks=BLOCKED",
                         style="green" if checks.get("blockingPassed") else "bold red")
            changes = d.file_changes()
            if changes:
                t.append(f" Δfiles={len(changes)}", style="yellow")
        if gate:
            t.append("  ⏳ needs your review — press a", style="bold yellow")
        self.query_one("#detail_header", Static).update(t)

    def _current_task(self, d: RunDetail) -> TaskNode | None:
        if self._selected_ref:
            for t in d.all_tasks():
                if t.ref == self._selected_ref:
                    return t
        busy = d.busiest_running_agent()
        if busy:
            return busy
        agents = d.coding_agents()
        if agents:
            return agents[-1]
        tasks = list(d.all_tasks())
        return tasks[-1] if tasks else None

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        data = event.node.data
        if isinstance(data, TaskNode):
            self._selected_ref = data.ref
            if self.detail and self.detail.run.running:
                self.query_one(TurnLog).show(data)

    # ------------------------------------------------------------------ HITL review gate
    def _check_gate(self, d: RunDetail) -> None:
        """Auto-open the approval modal the first time a run pauses at a review gate."""
        gate = d.pending_gate() if d.run.running else None
        if not gate or self._gate_open or gate.task_id in self._gate_prompted:
            return
        self._gate_prompted.add(gate.task_id)
        self._open_gate(gate)

    def _open_gate(self, gate: TaskNode) -> None:
        draft = (gate.input or {}).get("draft") or {}
        wf = gate.input.get("workflow") or (self.detail.run.workflow if self.detail else "")
        if wf == "feature_campaign":
            draft = dict(draft)
            draft.setdefault("phase", gate.input.get("phase"))
            draft.setdefault("wave", gate.input.get("wave"))
        # A design WAIT carries its isolated worktree path.  Passing it to the
        # modal lets the reviewer inspect the actual generated markdown before
        # approving, without trusting paths supplied in the draft itself.
        workspace_path = gate.input.get("repoPath")
        if not workspace_path and self.detail:
            workspace_path = self.detail.workspace()
        self._gate_open = True
        self.app.push_screen(ApprovalModal(
            wf, draft,
            pr_number=gate.input.get("prNumber"),
            issue_number=gate.input.get("issueNumber"),
            workspace_path=workspace_path,
            on_decision=lambda status, output: self._on_gate_decision(gate, status, output),
        ))

    def action_review_gate(self) -> None:
        gate = self.detail.pending_gate() if self.detail and self.detail.run.running else None
        if not gate or self._gate_open:
            self.app.bell()
            return
        self._open_gate(gate)

    def _on_gate_decision(self, gate: TaskNode, status, output) -> None:
        self._gate_open = False
        if status is None:                      # deferred — leave the run paused
            self.notify("review deferred — the run stays paused; press a to review")
            return
        self.signal_gate(gate, status, output)

    @work(group="mutate")
    async def signal_gate(self, gate: TaskNode, status: str, output: dict) -> None:
        try:
            # A pending gate may belong to a recursed SUB_WORKFLOW. Signal the workflow
            # that owns the checkpoint task, not necessarily the run opened in this screen.
            await self.app.client.signal_task(gate.workflow_id or self._id,
                                              gate.ref, status, output, task_type=gate.type)
            if gate.input.get("workflow") == "openspec_plan" and status == "COMPLETED":
                self.notify("plan approved — continuing…" if output.get("approved")
                            else "feedback submitted — revising the plan…")
            elif gate.input.get("workflow") == "feature_campaign":
                self.notify(f"campaign action submitted: {output.get('action', 'continue')}")
            elif gate.input.get("workflow") == "pr_review":
                action = output.get("action")
                self.notify(
                    "approved — posting comments and approving the PR…"
                    if action == "approve"
                    else "investigation requested — the private reviewer is digging deeper…"
                    if action == "investigate"
                    else "changes requested — posting your feedback without approval…"
                    if action == "revise"
                    else "review suppressed — nothing will be posted"
                )
            else:
                self.notify("approved — posting…" if status == "COMPLETED"
                            else "rejected — the run will fail")
        except ConductorError as e:
            self._gate_prompted.discard(gate.task_id)   # let them retry the decision
            self.notify(f"could not submit decision: {e}", severity="error")
        self.refresh_detail()

    def _maybe_notify(self, d: RunDetail) -> None:
        if self._id not in self.app.session_runs or self._id in self.app.notified:
            return
        self.app.notified.add(self._id)
        tokens, cost = d.tokens_cost()
        # Prefer the PR/review URL for terminal runs; fall back to the Conductor page.
        card = catalog.result_for(d.run.workflow, d.run.output)
        url = (card.primary_url if card and card.primary_url
               else self.app.settings.execution_url(self._id))
        notify.notify(self.app.settings.notify,
                      f"{d.run.workflow} — {d.run.status}",
                      f"{d.run.target} · {fmt.cost(cost)}",
                      url=url)

    # ------------------------------------------------------------------ actions
    def action_board(self) -> None:
        """Open the team board for this run (Phase 2, §4.8). The team cycle uses the run's workflowId
        as the board correlation id, so a team_build run's shared boards are addressable by ``self._id``."""
        from .board import TeamBoard
        self.app.push_screen(TeamBoard(self._board_run()))

    def _board_run(self) -> str:
        """Which run's board to open. team_build and cc_program own their board (keyed by their own id);
        an sdlc_intake front-door run hands the build to a child team_build, whose id keys the board."""
        if self.detail and self.detail.run.workflow == "sdlc_intake":
            for t in self.detail.tasks:
                if t.ref == "run_team" and t.sub_workflow_id:
                    return t.sub_workflow_id
        return self._id

    def action_back(self) -> None:
        if self._timer:
            self._timer.stop()
        self.app.pop_screen()

    def action_open_url(self) -> None:
        if self._primary_url:
            webbrowser.open(self._primary_url)
        else:
            self.app.bell()

    def action_open_folder(self) -> None:
        import os
        from .. import edit
        path = self.detail.workspace() if self.detail else None
        if not path or not os.path.isdir(path):
            self.notify("working dir not on this host (remote workers, or a cleaned /tmp clone)",
                        severity="warning")
            return
        self.notify(edit.open_path(self.app, path, self.app.settings.editor))

    def action_files(self) -> None:
        from ..widgets.modals import FileListModal
        changes = self.detail.file_changes() if self.detail else []
        if not changes:
            self.notify("no changed files reported for this run")
            return
        self.app.push_screen(FileListModal(changes, on_pick=self._open_file))

    def _open_file(self, rel_path: str) -> None:
        import os
        from .. import edit
        ws = self.detail.workspace() if self.detail else None
        if not ws:
            self.notify("working dir not on this host", severity="warning")
            return
        full = os.path.join(ws, rel_path)
        if not os.path.exists(full):
            self.notify(f"{rel_path} not found locally (deleted, or remote host)",
                        severity="warning")
            return
        self.notify(edit.open_path(self.app, full, self.app.settings.editor))

    def action_conductor(self) -> None:
        webbrowser.open(self.app.settings.execution_url(self._id))

    def action_yank(self) -> None:
        self.app.copy_to_clipboard(self._id)
        self.notify("workflow id copied")

    def action_logs(self) -> None:
        task = self._current_task(self.detail) if self.detail else None
        if not task or not task.task_id:
            self.app.bell()
            return
        self.run_logs(task)

    @work(group="logs")
    async def run_logs(self, task: TaskNode) -> None:
        lines = await self.app.client.task_logs(task.task_id)
        self.app.push_screen(LogsModal(f"{task.ref} ({task.def_name or task.type})", lines))

    def action_terminate(self) -> None:
        if not self.detail or not self.detail.run.running:
            self.app.bell()
            return
        run = self.detail.run
        forks = any(t.type == "SUB_WORKFLOW" and t.running for t in self.detail.all_tasks())
        msg = (f"Terminate {run.workflow} {fmt.short_id(run.id)} ({run.target})? "
               "Agents stop; the PR/branch keeps whatever was already pushed."
               + (" Sub-workflows are cascaded." if forks else ""))
        self.app.push_screen(ConfirmModal("Terminate run", msg, want_reason=True,
                                          confirm_label="Terminate",
                                          on_confirm=self._do_terminate))

    @work(group="mutate")
    async def _do_terminate(self, reason: str) -> None:
        try:
            await self.app.client.terminate(self._id, reason or "terminated from TUI")
            self.notify("terminate requested")
        except ConductorError as e:
            self.notify(f"terminate failed: {e}", severity="error")
        self.refresh_detail()

    def _child_of_program(self) -> bool:
        """A program's child team is driven by its program: retry or start over the program instead."""
        inp = (self.detail.run.input or {}) if self.detail else {}
        if inp.get("parentCorrelationId"):
            self.notify("This is one team of a program: retry or start over the program instead.",
                        severity="warning")
            return True
        return False

    def action_retry(self) -> None:
        """Resume a failed, timed-out or stopped run from the step that ended it; finished work is kept."""
        if not self.detail or self.detail.run.running:
            self.app.bell()
            return
        status = self.detail.run.status
        if self._child_of_program():
            return
        if status == "COMPLETED":
            self.retry_unfinished()  # the console decides: only a run that did not deliver is retried
            return
        if not (status.startswith("FAIL") or status in ("TIMED_OUT", "TERMINATED")):
            self.notify(f"Only a run that failed, was stopped or did not deliver can be retried (this one is "
                        f"{status.lower()}); R starts it over.", severity="warning")
            return
        self.run_retry()

    @work(group="mutate")
    async def retry_unfinished(self) -> None:
        import asyncio
        from .. import console as console_mod
        result = await asyncio.to_thread(console_mod.retry_run, self._id, server_url=self._server())
        if result.get("ok"):
            self.notify(f"retry started from this run's branch: {result['run'][:8]}")
        else:
            self.notify(result.get("error") or "the run was not retried", severity="warning")

    def action_start_over(self) -> None:
        """Start a NEW run with the same input. Conductor's restart would reuse the run id, and a swarm's
        board is keyed by it: the restarted run would read the old run's items and count its spend twice."""
        if not self.detail or self.detail.run.running:
            self.app.bell()
            return
        run = self.detail.run
        if self._child_of_program():
            return
        msg = (f"Start a new {run.workflow} run with the same request and settings, from the beginning and "
               f"with a fresh budget? {fmt.short_id(run.id)} stays as it is.")
        self.app.push_screen(ConfirmModal("Start over", msg, confirm_label="Start over",
                                          on_confirm=self._do_start_over))

    @work(group="mutate")
    async def _do_start_over(self, _reason: str = "") -> None:
        run = self.detail.run
        try:
            wid = await self.app.client.start(run.workflow, dict(run.input or {}))
        except ConductorError as e:
            self.notify(f"start over failed: {e}", severity="error")
            return
        self.notify(f"started over as {fmt.short_id(wid)}")
        self.app.switch_screen(RunDetail(wid))

    @work(group="mutate")
    async def run_retry(self) -> None:
        try:
            await self.app.client.retry(self._id)
            self.notify("retry requested")
            if self._timer:
                self._timer.stop()
            self._timer = self.set_interval(2.0, self.refresh_detail)
            self.app.notified.discard(self._id)
        except ConductorError as e:
            self.notify(f"retry failed: {e}", severity="error")

    # ---------------------------------------------------- the run's result branch
    # A finished run's work is a branch. F continues it with a follow-up run, P opens a pull request
    # (a GitHub remote is configured), M merges it into the branch the work started from. The same
    # logic as the web console's result card (console.run_result / prepare_continuation / ship_run).
    def _server(self) -> str:
        return getattr(self.app.client, "_base", "") or ""

    async def _result(self) -> dict | None:
        import asyncio
        from .. import console as console_mod
        status, wf = await asyncio.to_thread(
            console_mod._conductor, "GET", f"/workflow/{self._id}?includeTasks=true", server_url=self._server())
        if status != 200 or not isinstance(wf, dict):
            return None
        return await asyncio.to_thread(console_mod.run_result, {**wf, "workflowId": self._id},
                                       server_url=self._server())

    @work(group="mutate")
    async def action_continue_run(self) -> None:
        import asyncio
        from .. import console as console_mod
        result = await self._result()
        if not result or not (result.get("actions") or {}).get("continue"):
            self.notify("Only a finished run with a result branch can be continued.", severity="warning")
            return
        run = self.detail.run if self.detail else None
        workflow = run.workflow if run and run.workflow in console_mod.LAUNCH_TYPES else "sdlc_intake"
        try:
            values = await asyncio.to_thread(console_mod.prepare_continuation, self._id, {}, workflow,
                                             server_url=self._server())
        except Exception as e:  # noqa: BLE001 - say exactly what stands in the way
            self.notify(str(e), severity="error")
            return
        field = console_mod._TEXT_FIELD.get(workflow, "description")
        values[field] = values[field].rstrip() + "\n\n"  # the follow-up is typed after what it builds on
        from .launcher import LauncherForm
        self.app.push_screen(LauncherForm(workflow, values, continues=self._id))
        self.notify(f"Continuing from {result['branch']}; describe what to add at the end of the request.")

    @work(group="mutate")
    async def action_pull_request(self) -> None:
        import asyncio
        from .. import console as console_mod
        out = await asyncio.to_thread(console_mod.ship_run, self._id, "pr", author="human:tui",
                                      server_url=self._server())
        if out.get("ok"):
            self.notify(f"Pushing the branch and opening the pull request ({fmt.short_id(out['run'])}).")
        else:
            self.notify(out.get("error") or "The pull request was not started.", severity="error")

    @work(group="mutate")
    async def action_merge(self) -> None:
        result = await self._result()
        if not result or not (result.get("actions") or {}).get("merge"):
            self.notify("This run's branch has nothing left to merge.", severity="warning")
            return
        msg = (f"Merge {result['branch']} into {result['base']} in {result['repo']}? A fast-forward when "
               f"{result['base']} has not moved, otherwise a merge commit. A dirty checkout or a conflict "
               "changes nothing.")
        self.app.push_screen(ConfirmModal("Merge", msg, confirm_label="Merge", on_confirm=self._do_merge))

    @work(group="mutate")
    async def _do_merge(self, _reason: str = "") -> None:
        import asyncio
        from .. import console as console_mod
        out = await asyncio.to_thread(console_mod.ship_run, self._id, "merge", author="human:tui",
                                      server_url=self._server())
        if out.get("ok"):
            self.notify("Already merged." if out.get("already") else
                        f"Merged into {out['into']}{' (fast-forward)' if out.get('fastForward') else ''}.")
        else:
            self.notify(out.get("error") or "Not merged.", severity="error")

    def action_rerun(self) -> None:
        if not self.detail:
            return
        run = self.detail.run
        if run.workflow not in catalog.CATALOG:
            self.app.bell()
            return
        from .launcher import LauncherForm
        self.app.push_screen(LauncherForm(run.workflow, dict(run.input or {})))
