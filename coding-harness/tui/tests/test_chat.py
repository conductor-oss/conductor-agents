"""Chat foundation tests — session store, tool dispatcher, and the llm loop (stubbed)."""

from __future__ import annotations

import asyncio

import pytest

from tui import api, templates
from tui.chat import llm, prompt, tools
from tui.chat.session import Session, SessionStore
from tui.tests.test_screens import FakeClient


# --------------------------------------------------------------------------- session

def test_session_roundtrip(tmp_path):
    store = SessionStore(tmp_path)
    s = Session.new("claude-sonnet-4-6")
    s.set_title_from("review PR 7 on acme/app")
    s.messages.append({"role": "user", "content": "hi"})
    s.messages.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    s.add_run("wf-1"); s.add_run("wf-1")  # dedupe
    store.save(s)
    loaded = store.load(s.id)
    assert loaded.title == "review PR 7 on acme/app"
    assert loaded.messages == s.messages
    assert loaded.runs == ["wf-1"]
    assert store.list()[0].id == s.id
    assert store.latest().id == s.id


# --------------------------------------------------------------------------- tools

def _ctx(client, confirm_result=True, started=None):
    started = started if started is not None else []
    async def confirm(title, message):
        return confirm_result
    return tools.ToolContext(client=client, confirm=confirm,
                             on_run_started=lambda wid: started.append(wid)), started


@pytest.mark.asyncio
async def test_start_workflow_requires_inputs_then_confirms():
    fc = FakeClient()
    ctx, started = _ctx(fc)
    # missing required prNumber
    out = await tools.dispatch("start_workflow", {"workflow": "pr_review", "inputs": {"repo": "acme/app"}}, ctx)
    assert "missing required inputs" in out and "prNumber" in out
    assert not fc.started
    # complete → confirmed → started
    out = await tools.dispatch("start_workflow",
                               {"workflow": "pr_review", "inputs": {"repo": "acme/app", "prNumber": 7}}, ctx)
    assert "started pr_review" in out and "wf-new-123" in out
    # chat launches pr_review with the review gate on by default (approve injected)
    assert fc.started == [("pr_review", {"repo": "acme/app", "prNumber": 7, "approve": True})]
    assert started == ["wf-new-123"]


@pytest.mark.asyncio
async def test_start_injects_gate_default_unless_overridden():
    fc = FakeClient()
    ctx, _ = _ctx(fc)
    # explicit approve=False is respected (unattended run)
    await tools.dispatch("start_workflow",
                         {"workflow": "pr_review",
                          "inputs": {"repo": "acme/app", "prNumber": 7, "approve": False}}, ctx)
    assert fc.started[-1] == ("pr_review", {"repo": "acme/app", "prNumber": 7, "approve": False})
    # A later user turn gets a fresh context; issue_to_pr gets approvePr by default.
    ctx, _ = _ctx(fc)
    await tools.dispatch("start_workflow",
                         {"workflow": "issue_to_pr", "inputs": {"repo": "acme/app", "issueNumber": 3,
                                                                  "design": False}}, ctx)
    assert fc.started[-1] == ("issue_to_pr", {"repo": "acme/app", "issueNumber": 3,
                                               "design": False, "approvePr": True})


@pytest.mark.asyncio
async def test_chat_start_consults_user_template_library():
    path = templates.save(
        "Fix PR feedback", "Use this exact fix template: {{feedback}}",
        workflows=("address_pr",))
    fc = FakeClient()
    ctx, _ = _ctx(fc)
    out = await tools.dispatch(
        "start_workflow",
        {"workflow": "address_pr",
         "inputs": {"repo": "acme/app", "prNumber": 7, "design": False}},
        ctx,
    )
    payload = fc.started[-1][1]
    assert payload["fixPromptTemplate"] == "Use this exact fix template: {{feedback}}"
    assert payload["fixPromptTemplateSource"] == f"user:{path}"
    assert f"fixPromptTemplate=user:{path}" in out


@pytest.mark.asyncio
async def test_chat_start_blocks_ambiguous_template_role():
    for name in ("Fix one", "Fix two"):
        templates.save(name, name, workflows=("address_pr",))
    fc = FakeClient()
    ctx, _ = _ctx(fc)
    out = await tools.dispatch(
        "start_workflow",
        {"workflow": "address_pr",
         "inputs": {"repo": "acme/app", "prNumber": 7, "design": False}},
        ctx,
    )
    assert "multiple equally specific templates" in out
    assert "No workflow was started" in out
    assert not fc.started


@pytest.mark.asyncio
async def test_chat_schedule_consults_user_template_library():
    path = templates.save(
        "Scheduled review", "SCHEDULED REVIEW {{diff}}",
        workflows=("pr_review_sweep",))

    class ScheduleClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.saved_schedules = []

        async def save_schedule(self, payload):
            self.saved_schedules.append(payload)

    fc = ScheduleClient()
    ctx, _ = _ctx(fc)
    out = await tools.dispatch(
        "save_schedule",
        {"workflow": "pr_review_sweep", "repo": "acme/app"},
        ctx,
    )
    assert "saved schedule" in out
    inputs = fc.saved_schedules[0]["startWorkflowRequest"]["input"]
    assert inputs["reviewPromptTemplate"] == "SCHEDULED REVIEW {{diff}}"
    assert inputs["reviewPromptTemplateSource"] == f"user:{path}"


@pytest.mark.asyncio
async def test_code_parallel_paths_require_explicit_design_choice():
    fc = FakeClient()
    ctx, _ = _ctx(fc)
    out = await tools.dispatch("start_workflow", {"workflow": "code_parallel", "inputs": {"repoPath": "/tmp/repo", "instruction": "fix it"}}, ctx)
    assert "design choice required" in out and not fc.started
    out = await tools.dispatch("start_workflow", {"workflow": "code_parallel", "inputs": {"repoPath": "/tmp/repo", "instruction": "fix it", "design": False}}, ctx)
    assert "started code_parallel" in out and fc.started[-1][1]["design"] is False


@pytest.mark.asyncio
async def test_start_workflow_declined():
    fc = FakeClient()
    ctx, _ = _ctx(fc, confirm_result=False)
    out = await tools.dispatch("start_workflow",
                               {"workflow": "pr_review", "inputs": {"repo": "acme/app", "prNumber": 7}}, ctx)
    assert "declined" in out and not fc.started


@pytest.mark.asyncio
async def test_start_workflow_blocks_stale_registered_definition():
    fc = FakeClient()

    async def stale(_name):
        return False

    fc.workflow_registered = stale
    ctx, _ = _ctx(fc)
    out = await tools.dispatch(
        "start_workflow",
        {"workflow": "pr_review", "inputs": {"repo": "acme/app", "prNumber": 7}},
        ctx,
    )
    assert "missing or stale" in out
    assert "Run /register" in out
    assert not fc.started


@pytest.mark.asyncio
async def test_start_workflow_allows_only_one_per_user_turn():
    fc = FakeClient()
    ctx, started = _ctx(fc)
    first = await tools.dispatch(
        "start_workflow",
        {"workflow": "pr_review", "inputs": {"repo": "acme/app", "prNumber": 7}},
        ctx,
    )
    second = await tools.dispatch(
        "start_workflow",
        {"workflow": "address_pr", "inputs": {"repo": "acme/app", "prNumber": 7}},
        ctx,
    )
    assert "started pr_review" in first
    assert "already been started" in second
    assert len(fc.started) == 1
    assert started == ["wf-new-123"]


@pytest.mark.asyncio
async def test_register_workflows_uses_selected_server(monkeypatch):
    from tui import registration

    seen = {}

    async def fake_register(server_url):
        seen["server_url"] = server_url
        return registration.RegistrationResult(True, "registered=all worker_gate=ok")

    monkeypatch.setattr(registration, "register_definitions", fake_register)
    fc = FakeClient()
    ctx, _ = _ctx(fc)
    ctx.server_url = "http://selected:8080/api"
    out = await tools.dispatch("register_workflows", {}, ctx)
    assert "registration complete" in out
    assert seen == {"server_url": "http://selected:8080/api"}


@pytest.mark.asyncio
async def test_terminate_confirms():
    fc = FakeClient()
    ctx, _ = _ctx(fc)
    out = await tools.dispatch("terminate_run", {"workflow_id": "w9"}, ctx)
    assert "terminated w9" in out and fc.terminated == [("w9", "terminated from chat")]


@pytest.mark.asyncio
async def test_list_runs_shape():
    runs = [api.Run(id="a" * 8, workflow="pr_review", status="COMPLETED", start_ms=1, end_ms=2,
                    input={"repo": "acme/app", "prNumber": 7})]
    fc = FakeClient(runs=runs)
    ctx, _ = _ctx(fc)
    out = await tools.dispatch("list_runs", {}, ctx)
    assert "pr_review" in out and "COMPLETED" in out


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "suppressed"), [("revise", False), ("stop", True)])
async def test_chat_revise_and_stop_fail_approval_closed(action, suppressed):
    class ApprovalClient(FakeClient):
        async def pending_approvals(self):
            return [api.PendingApproval(
                task_id="task-1", task_ref="review_gate", task_type="WAIT",
                workflow_id="wf-child", workflow="pr_review",
                input={"draft": {"summary": "draft"}}, scheduled_ms=1)]

    fc = ApprovalClient()
    ctx, _ = _ctx(fc)
    payload = {"task_id": "task-1", "action": action}
    if action == "revise":
        payload["feedback"] = "Correct the line anchor."
    out = await tools.dispatch("decide_approval", payload, ctx)
    assert f"{action} recorded" in out
    wid, ref, status, output = fc.signals[-1]
    assert (wid, ref, status) == ("wf-child", "review_gate", "COMPLETED")
    assert output["suppressed"] is suppressed


def test_prompt_mentions_workflows():
    p = prompt.system_prompt("http://localhost:8080/api")
    for wf in ("pr_review", "issue_to_pr", "address_pr", "code_parallel"):
        assert wf in p
    assert "localhost:8080" in p
    assert "at most ONE workflow" in p
    assert "register_workflows" in p
    assert "ambiguous" in p
    assert "explicitly ask whether the user wants design docs" in p
    assert "isolated git worktree" in p


# --------------------------------------------------------------------------- llm loop (stubbed)

class _Blk:
    def __init__(self, **kw): self.__dict__.update(kw)


class _Usage:
    def __init__(self, i, o): self.input_tokens, self.output_tokens = i, o


class _FinalMsg:
    def __init__(self, content, stop_reason, usage):
        self.content, self.stop_reason, self.usage = content, stop_reason, usage


class _Stream:
    def __init__(self, texts, final):
        self._texts, self._final = texts, final
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    @property
    def text_stream(self):
        async def gen():
            for t in self._texts:
                yield t
        return gen()
    async def get_final_message(self): return self._final


class _StubClient:
    """Two-call script: first a tool_use turn, then a text turn."""
    def __init__(self): self._calls = 0
    class _Messages:
        def __init__(self, outer): self._outer = outer
        def stream(self, **kw):
            c = self._outer._calls
            self._outer._calls += 1
            if c == 0:
                final = _FinalMsg(
                    [_Blk(type="tool_use", id="t1", name="list_runs", input={})],
                    "tool_use", _Usage(10, 5))
                return _Stream([], final)
            final = _FinalMsg([_Blk(type="text", text="Here are your runs.")],
                              "end_turn", _Usage(8, 12))
            return _Stream(["Here are ", "your runs."], final)
    @property
    def messages(self): return _StubClient._Messages(self)


class _StartStub:
    """First turn calls start_workflow(pr_review), then a text turn."""
    def __init__(self): self._calls = 0
    class _Messages:
        def __init__(self, outer): self._outer = outer
        def stream(self, **kw):
            c = self._outer._calls
            self._outer._calls += 1
            if c == 0:
                tu = _Blk(type="tool_use", id="s1", name="start_workflow",
                          input={"workflow": "pr_review", "inputs": {"repo": "acme/app", "prNumber": 7}})
                return _Stream([], _FinalMsg([tu], "tool_use", _Usage(5, 5)))
            return _Stream(["Started it."], _FinalMsg([_Blk(type="text", text="Started it.")],
                                                      "end_turn", _Usage(3, 3)))
    @property
    def messages(self): return _StartStub._Messages(self)


class _AmbiguousStartStub:
    """First turn requests two starts; the engine must execute neither."""
    def __init__(self): self._calls = 0
    class _Messages:
        def __init__(self, outer): self._outer = outer
        def stream(self, **kw):
            c = self._outer._calls
            self._outer._calls += 1
            if c == 0:
                starts = [
                    _Blk(type="tool_use", id="s1", name="start_workflow",
                         input={"workflow": "pr_review", "inputs": {}}),
                    _Blk(type="tool_use", id="s2", name="start_workflow",
                         input={"workflow": "address_pr", "inputs": {}}),
                ]
                return _Stream([], _FinalMsg(starts, "tool_use", _Usage(5, 5)))
            return _Stream(["Which one?"], _FinalMsg([_Blk(type="text", text="Which one?")],
                                                     "end_turn", _Usage(3, 3)))
    @property
    def messages(self): return _AmbiguousStartStub._Messages(self)


def _chat_app(conductor_client, llm_stub):
    from tui.app import HarnessApp
    from tui.config import Settings
    app = HarnessApp(Settings(server_url="http://x/api", notify=False), client=conductor_client)
    app.llm_client = llm_stub
    return app


@pytest.mark.asyncio
async def test_chat_readonly_turn_no_confirm():
    from tui.tests.test_screens import FakeClient
    runs = [api.Run(id="r" * 8, workflow="pr_review", status="COMPLETED", start_ms=1, end_ms=2,
                    input={"repo": "acme/app", "prNumber": 7})]
    fc = FakeClient(runs=runs)
    app = _chat_app(fc, _StubClient())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.2)
        app.screen.query_one("#chat_input").value = "list my runs"
        await pilot.press("enter")
        await pilot.pause(1.0)
        roles = [m["role"] for m in app.session.messages]
        assert roles == ["user", "assistant", "user", "assistant"]  # user, tool_use, tool_result, text
        assert type(app.screen).__name__ == "Chat"  # no modal (read-only)
        assert fc.started == []


@pytest.mark.asyncio
async def test_chat_start_workflow_confirms_and_starts():
    from tui.tests.test_screens import FakeClient
    from textual.widgets import Button
    fc = FakeClient()
    app = _chat_app(fc, _StartStub())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.2)
        app.screen.query_one("#chat_input").value = "review PR 7 on acme/app"
        await pilot.press("enter")
        await pilot.pause(0.6)
        # a confirm modal should be up
        assert type(app.screen).__name__ == "ConfirmModal"
        await pilot.click("#ok")
        await pilot.pause(0.8)
        assert fc.started == [("pr_review", {"repo": "acme/app", "prNumber": 7, "approve": True})]
        assert app.session.runs == ["wf-new-123"]


@pytest.mark.asyncio
async def test_llm_loop_dispatches_tool_then_finishes():
    eng = llm.ChatEngine(_StubClient(), "claude-sonnet-4-6", "sys")
    messages = [{"role": "user", "content": "list my runs"}]
    texts, tool_calls, tool_dones = [], [], []
    async def run_tool(name, inp):
        tool_calls.append((name, inp))
        return "run-1 COMPLETED pr_review acme/app"
    res = await eng.run(
        messages, run_tool=run_tool,
        on_text=texts.append,
        on_tool_start=lambda n, i: None,
        on_tool_done=lambda n, o: tool_dones.append((n, o)),
    )
    assert tool_calls == [("list_runs", {})]
    assert "".join(texts) == "Here are your runs."
    # messages now: user, assistant(tool_use), user(tool_result), assistant(text)
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[2]["content"][0]["type"] == "tool_result"
    assert res["tokens"] == 10 + 5 + 8 + 12
    assert res["cost"] > 0


@pytest.mark.asyncio
async def test_llm_loop_rejects_ambiguous_multi_start_batch():
    eng = llm.ChatEngine(_AmbiguousStartStub(), "claude-sonnet-4-6", "sys")
    messages = [{"role": "user", "content": "review and address this PR"}]
    executed = []

    async def run_tool(name, inp):
        executed.append((name, inp))
        return "should not run"

    await eng.run(
        messages, run_tool=run_tool,
        on_text=lambda text: None,
        on_tool_start=lambda name, inp: None,
        on_tool_done=lambda name, out: None,
    )
    assert executed == []
    results = messages[2]["content"]
    assert len(results) == 2
    assert all("no workflow was started" in result["content"] for result in results)
