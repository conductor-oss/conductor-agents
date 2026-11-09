"""Board-aware TUI layer (roadmap Phase 2, §4.8): projection + steering over the shared boards.

The TUI reuses the workers' pure board read-model, so these tests assert the projection renders the
kanban/rollup and that each steering action is a real authored board write the next standup can read.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tui"))
sys.path.insert(0, str(ROOT / "workers"))

import board as tui_board  # noqa: E402
from common import team_board as tb  # noqa: E402


def _isolate(monkeypatch, tmp_path):
    d = str(tmp_path / "bb")
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", d)
    return d


def test_project_and_render_board(monkeypatch, tmp_path):
    bb = _isolate(monkeypatch, tmp_path)
    cid = "run-ui"
    tb.post_item(cid, "lead", {"id": "email", "title": "Email channel", "files": ["registry.py"]})
    tb.post_item(cid, "lead", {"id": "sms", "title": "SMS channel", "files": ["registry.py"]})
    tb.set_state(cid, "lead", "email", tb.DONE)
    tb.set_state(cid, "lead", "sms", tb.IN_PROGRESS, assignee="sms")
    tb.post_lesson(cid, "email", "registry uses append-only register()", confidence="high")

    view = tui_board.project_board(cid, blackboard_dir=bb)
    assert view["rollup"]["done"] == 1 and view["rollup"]["total"] == 2
    # in_progress sorts before done.
    assert [it["id"] for it in view["items"]] == ["sms", "email"]


def test_steering_writes_reach_the_next_standup(monkeypatch, tmp_path):
    bb = _isolate(monkeypatch, tmp_path)
    cid = "run-steer"
    tb.post_item(cid, "lead", {"id": "sms", "title": "SMS channel"})

    # A human nudge + reprioritize + reopen are all authored board writes.
    tui_board.nudge_item(cid, "sms", "do not use eval()", blackboard_dir=bb)
    tui_board.reprioritize(cid, "sms", 9, blackboard_dir=bb)
    tui_board.reopen_item(cid, "sms", blackboard_dir=bb)

    board = tb.fold_board(cid)
    # The directive is on the board, authored by human with override authority.
    d = board["directives"][-1]
    assert d["target"] == "sms" and d["authority"] == "human" and "eval" in d["text"]
    # Reprioritize folded through; reopen made it dispatchable again.
    assert board["items"]["sms"]["priority"] == 9
    assert board["items"]["sms"]["state"] == tb.REOPENED
    assert [it["id"] for it in tb.ready_items(board["items"])] == ["sms"]


# ─────────────── the Textual screen drives the same writes as the web console ───────────────
import pytest  # noqa: E402

from tui.app import HarnessApp  # noqa: E402
from tui.config import Settings  # noqa: E402
from tui.screens.board import TeamBoard  # noqa: E402
from tui.tests.test_screens import FakeClient  # noqa: E402


@pytest.mark.asyncio
async def test_tui_board_screen_steers_as_registered_human(monkeypatch, tmp_path):
    bb = _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SWARM_ACTOR_TOKEN", "test-token")
    cid = "run-tui"
    tb.post_item(cid, "lead", {"id": "api", "title": "API layer"})
    tb.post_item(cid, "lead", {"id": "db", "title": "DB layer"})
    tb.set_state(cid, "lead", "api", tb.BLOCKED, reason="review_rounds_exhausted")
    tb.post_exception(cid, "monitor", "api", "review_rounds_exhausted", "3 rejections")
    tb.post_directive(cid, "human:ops@example.com", "db", "no ORM", authority="human")
    old = tb.fold_board(cid)["directives"][-1]["id"]

    screen = TeamBoard(cid, bb)
    app = HarnessApp(Settings(server_url="http://x/api", notify=False), client=FakeClient())
    async with app.run_test(size=(160, 50)) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.author == "human:ops@example.com"
        assert [it["id"] for it in screen._items] == ["db", "api"]  # ready sorts before blocked
        assert len(screen._constraints) == 1
        assert screen.focused is screen.query_one("#board_directive")  # message line first
        await pilot.press("escape")  # → item list, where the shortcuts act
        assert screen.focused is screen.query_one("#board_items")
        # Stop the highlighted item (db) with a reason → a blocking directive authored by the human.
        await pilot.press("s")
        for ch in "wrong table":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await pilot.pause()
        # Send the quarantined item (api) back for rework: feedback, not an approval.
        screen.query_one("#board_items").index = 1
        await pilot.press("o")
        await pilot.pause()
        # Lift the original constraint from the constraints list.
        screen.query_one("#board_constraints").focus()
        screen.query_one("#board_constraints").index = [d["id"] for d in screen._constraints].index(old)
        await pilot.press("l")
        await pilot.pause()

    board = tb.fold_board(cid)
    stop = [d for d in board["directives"] if d.get("urgency") == "blocking"][-1]
    assert stop["target"] == "db" and stop["text"] == "wrong table" and stop["by"] == "human:ops@example.com"
    assert board["items"]["api"]["state"] == tb.REOPENED
    assert old not in {d["id"] for d in tb.active_directives(board["directives"])}


# ─────────────── plain-language steering: just send the message ───────────────
import steer_message  # noqa: E402


def _seed_routing_board(cid):
    tb.post_item(cid, "lead", {"id": "circle", "title": "circle area"})
    tb.post_item(cid, "lead", {"id": "square", "title": "square area"})
    tb.set_state(cid, "lead", "square", tb.IN_PROGRESS)
    tb.post_directive(cid, "human:ops@example.com", "circle", "use 3.14 for pi", authority="human")
    return tb.fold_board(cid)["directives"][-1]["id"]


def test_message_is_routed_verbatim_and_validated(monkeypatch, tmp_path):
    bb = _isolate(monkeypatch, tmp_path)
    cid = "run-msg"
    old = _seed_routing_board(cid)
    seen = {}

    def fake(message, ctx, client=None):
        seen["ctx"] = ctx
        return {"reply": "Stopping square and replacing the pi rule.",
                "actions": [{"action": "stop", "run": cid, "item": "square", "directive": ""},
                            {"action": "replace", "run": cid, "item": "", "directive": old},
                            {"action": "accept", "run": cid, "item": "ghost", "directive": ""}]}
    monkeypatch.setattr(steer_message, "_ask_claude", fake)
    msg = "stop square, it is wrong; and use math.pi, not 3.14"
    res = steer_message.send(cid, msg, author="human:ops@example.com", blackboard_dir=bb)

    assert res["ok"] and res["interpretedBy"] == "claude"
    assert [(a["action"], a["item"]) for a in res["actions"]] == [("stop", "square"), ("replace", "circle")]
    assert {i["id"] for i in seen["ctx"]["items"]} == {"circle", "square"}
    board = tb.fold_board(cid)
    stop = [d for d in board["directives"] if d.get("urgency") == "blocking"][-1]
    assert stop["target"] == "square" and stop["text"] == msg and stop["by"] == "human:ops@example.com"
    live = {d["id"]: d for d in tb.active_directives(board["directives"])}
    assert old not in live and any(d["text"] == msg and d["target"] == "circle" for d in live.values())
    assert "stop → square" in steer_message.describe(res)


def test_message_falls_back_to_the_whole_team(monkeypatch, tmp_path):
    bb = _isolate(monkeypatch, tmp_path)
    cid = "run-msg-fb"
    _seed_routing_board(cid)

    def broken(message, ctx, client=None):
        raise ConnectionError("no network")
    monkeypatch.setattr(steer_message, "_ask_claude", broken)
    res = steer_message.send(cid, "prefer small functions", author="human:ops@example.com", blackboard_dir=bb)
    assert res["ok"] and res["interpretedBy"] == "fallback" and "ConnectionError" in res["note"]
    d = tb.fold_board(cid)["directives"][-1]
    assert d["target"] == "team" and d["text"] == "prefer small functions"


def test_console_message_action_requires_token_and_routes(monkeypatch, tmp_path):
    import json as _json
    import console
    bb = _isolate(monkeypatch, tmp_path)
    cid = "run-msg-console"
    _seed_routing_board(cid)
    monkeypatch.setattr(steer_message, "_ask_claude", lambda m, c, client=None: {
        "reply": "Guidance for circle.", "actions": [{"action": "nudge", "run": cid, "item": "circle", "directive": ""}]})
    body = _json.dumps({"run": cid, "action": "message", "text": "add a docstring"}).encode()
    status, _, _ = console.handle_post("/api/steer", body, bb, headers={})
    assert status == 401
    status, _, raw = console.handle_post("/api/steer", body, bb, headers={"Authorization": "Bearer test-token"})
    out = _json.loads(raw)
    assert status == 200 and out["ok"] and "nudge → circle" in out["summary"]
    assert tb.fold_board(cid)["directives"][-1]["target"] == "circle"


@pytest.mark.asyncio
async def test_tui_board_message_line_sends_plain_language(monkeypatch, tmp_path):
    bb = _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SWARM_ACTOR_TOKEN", "test-token")
    cid = "run-msg-tui"
    _seed_routing_board(cid)
    monkeypatch.setattr(steer_message, "_ask_claude", lambda m, c, client=None: {
        "reply": "Stopping square.", "actions": [{"action": "stop", "run": cid, "item": "square", "directive": ""}]})
    screen = TeamBoard(cid, bb)
    app = HarnessApp(Settings(server_url="http://x/api", notify=False), client=FakeClient())
    async with app.run_test(size=(160, 50)) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        for ch in "halt square":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
    stop = [d for d in tb.fold_board(cid)["directives"] if d.get("urgency") == "blocking"][-1]
    assert stop["target"] == "square" and stop["text"] == "halt square" and stop["by"] == "human:ops@example.com"


@pytest.mark.asyncio
async def test_chat_message_swarm_finds_the_one_active_swarm(monkeypatch, tmp_path):
    """In chat the human just says it: the tool finds the single active swarm run (an sdlc_intake
    routed to a team → that team's board) and delivers the words verbatim, no confirmation."""
    from tui import api
    from tui.chat import tools
    bb = _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SWARM_ACTOR_TOKEN", "test-token")
    _seed_routing_board("team-1")
    monkeypatch.setattr(steer_message, "_ask_claude", lambda m, c, client=None: {
        "reply": "Guidance for circle.", "actions": [{"action": "nudge", "run": "team-1", "item": "circle", "directive": ""}]})
    intake = api.Run(id="intake-1", workflow="sdlc_intake", status="RUNNING", start_ms=1, end_ms=None)
    child = api.Run(id="team-1", workflow="team_build", status="RUNNING", start_ms=2, end_ms=None)

    class Client(FakeClient):
        async def search_runs(self, limit=50):
            return [intake, child, api.Run(id="old", workflow="team_build", status="COMPLETED", start_ms=0, end_ms=1)]

        async def get_run(self, wid, recurse=True, only_running=False):
            run = intake if wid == "intake-1" else child
            tasks = [api.TaskNode(ref="run_team", def_name="team_build", type="SUB_WORKFLOW", status="IN_PROGRESS",
                                  task_id="t", output={}, sub_workflow_id="team-1")] if wid == "intake-1" else []
            return api.RunDetail(run=run, tasks=tasks)

    async def never(title, message):
        raise AssertionError("steering must not ask for confirmation")
    ctx = tools.ToolContext(client=Client(), confirm=never, on_run_started=lambda _id: None)
    out = await tools.dispatch("message_swarm", {"message": "circle needs a docstring"}, ctx)
    assert out.startswith("delivered to run team-1") and "nudge → circle" in out
    d = tb.fold_board("team-1")["directives"][-1]
    assert d["target"] == "circle" and d["text"] == "circle needs a docstring" and d["by"] == "human:ops@example.com"
    del bb


def test_routed_reopen_carries_the_message(monkeypatch, tmp_path):
    bb = _isolate(monkeypatch, tmp_path)
    cid = "run-msg-reopen"
    _seed_routing_board(cid)
    tb.set_state(cid, "lead", "circle", tb.DONE)
    monkeypatch.setattr(steer_message, "_ask_claude", lambda m, c, client=None: {
        "reply": "Reopening circle.", "actions": [{"action": "reopen", "run": cid, "item": "circle", "directive": ""}]})
    res = steer_message.send(cid, "circle must reject a negative radius", author="human:ops@example.com",
                             blackboard_dir=bb)
    board = tb.fold_board(cid)
    assert res["ok"] and board["items"]["circle"]["state"] == tb.REOPENED
    d = board["directives"][-1]
    assert d["target"] == "circle" and d["text"] == "circle must reject a negative radius"


def test_not_team_work_posts_nothing(monkeypatch, tmp_path):
    """A request the coding agents cannot carry out ("start the server for me") is answered, not posted
    as a constraint an agent would have to push back on."""
    bb = _isolate(monkeypatch, tmp_path)
    cid = "run-msg-none"
    _seed_routing_board(cid)
    before = len(tb.fold_board(cid)["directives"])
    monkeypatch.setattr(steer_message, "_ask_claude", lambda m, c, client=None: {
        "reply": "Not sent: starting the server is not something the coding agents can do.",
        "actions": [{"action": "none", "run": cid, "item": "", "directive": ""}]})
    res = steer_message.send(cid, "when ready, start the server on port 1234", author="human:ops@example.com",
                             blackboard_dir=bb)
    assert res["ok"] and res["actions"] == [] and len(tb.fold_board(cid)["directives"]) == before
    assert "not sent to the team" in steer_message.describe(res)


# ─────────────── state on demand: snapshot, questions, API, chat ───────────────
import swarm_state  # noqa: E402


class _FakeAnthropic:
    """Records the request; answers with fixed text (no network)."""

    def __init__(self, text):
        self.text, self.requests = text, []
        outer = self

        class _Messages:
            def create(self, **kw):
                outer.requests.append(kw)
                block = type("B", (), {"type": "text", "text": outer.text})()
                return type("R", (), {"stop_reason": "end_turn", "content": [block]})()
        self.beta = type("Beta", (), {"messages": _Messages()})()


def _no_conductor(req):
    raise OSError("no server in tests")


def test_snapshot_reports_phase_progress_live_agents_and_escalations(monkeypatch, tmp_path):
    from swarm_substrate import blackboard as bbx
    bb = _isolate(monkeypatch, tmp_path)
    cid = "run-state"
    _seed_routing_board(cid)
    tb.post_exception(cid, "monitor", "circle", "blocked", "go vet failed")
    bbx.post_fact(cid, "implementer:square", "activity", {"item": "square", "toolCalls": 7,
                                                          "tool": "Edit square.go"})
    snap = swarm_state.snapshot(cid, blackboard_dir=bb, opener=_no_conductor)
    assert snap["phase"] == "building (0/2 done)" and snap["progress"] == {"done": 0, "total": 2}
    assert [(a["item"], a["tool"], a["toolCalls"]) for a in snap["activeAgents"]] == [("square", "Edit square.go", 7)]
    assert snap["escalations"][0]["kind"] == "blocked" and snap["constraints"][0]["text"] == "use 3.14 for pi"
    assert "1 agent(s) working: square (Edit square.go)" in snap["summary"] and "1 escalation(s)" in snap["summary"]


def test_ask_answers_from_the_snapshot_with_named_item_timelines(monkeypatch, tmp_path):
    bb = _isolate(monkeypatch, tmp_path)
    cid = "run-ask"
    _seed_routing_board(cid)
    fake = _FakeAnthropic("square is in progress; circle has not started.")
    monkeypatch.setattr(swarm_state, "_workflow", lambda *a, **k: {})
    got = swarm_state.ask(cid, "what is square doing?", blackboard_dir=bb, client=fake)
    assert got["ok"] and got["answer"].startswith("square is in progress")
    sent = fake.requests[0]["messages"][0]["content"]
    assert '"square"' in sent and "Item timelines" in sent and "what is square doing?" in sent


def test_a_question_in_the_message_box_is_answered_not_posted(monkeypatch, tmp_path):
    bb = _isolate(monkeypatch, tmp_path)
    cid = "run-q"
    _seed_routing_board(cid)
    before = len(tb.fold_board(cid)["directives"])
    monkeypatch.setattr(steer_message, "_ask_claude", lambda m, c, client=None: {
        "reply": "", "actions": [{"action": "answer", "run": cid, "item": "", "directive": ""}]})
    monkeypatch.setattr(swarm_state, "ask", lambda run, q, **k: {"ok": True, "answer": "2 items, none done yet."})
    res = steer_message.send(cid, "where are we?", author="human:ops@example.com", blackboard_dir=bb)
    assert res["ok"] and res["answer"] == "2 items, none done yet." and res["actions"] == []
    assert len(tb.fold_board(cid)["directives"]) == before
    assert "2 items, none done yet." in steer_message.describe(res)


def test_console_state_is_open_and_ask_requires_a_token(monkeypatch, tmp_path):
    import json as _json
    import console
    bb = _isolate(monkeypatch, tmp_path)
    cid = "run-api"
    _seed_routing_board(cid)
    monkeypatch.setattr(swarm_state, "_workflow", lambda *a, **k: {})
    status, _, raw = console.handle_get("/api/state", {"run": [cid]}, bb)
    assert status == 200 and _json.loads(raw)["progress"]["total"] == 2
    body = _json.dumps({"run": cid, "question": "how is it going?"}).encode()
    assert console.handle_post("/api/ask", body, bb, headers={})[0] == 401
    monkeypatch.setattr(swarm_state, "ask", lambda run, q, **k: {"ok": True, "answer": f"{run}: fine"})
    status, _, raw = console.handle_post("/api/ask", body, bb, headers={"Authorization": "Bearer test-token"})
    assert status == 200 and _json.loads(raw)["answer"] == f"{cid}: fine"


@pytest.mark.asyncio
async def test_chat_swarm_state_answers_for_the_one_active_swarm(monkeypatch, tmp_path):
    from tui import api
    from tui import swarm_state as pkg_state
    from tui.chat import tools
    _isolate(monkeypatch, tmp_path)
    run = api.Run(id="team-9", workflow="team_build", status="RUNNING", start_ms=1, end_ms=None)

    class Client(FakeClient):
        async def search_runs(self, limit=50):
            return [run]

        async def get_run(self, wid, recurse=True, only_running=False):
            return api.RunDetail(run=run, tasks=[])

    monkeypatch.setattr(pkg_state, "ask", lambda r, q, **k: {"ok": True, "answer": f"{r} is on wave 2"})
    ctx = tools.ToolContext(client=Client(), confirm=None, on_run_started=lambda _id: None)
    assert await tools.dispatch("swarm_state", {"question": "how far along?"}, ctx) == "team-9 is on wave 2"


def test_snapshot_carries_the_runs_retrospective_once_it_ran(monkeypatch, tmp_path):
    from swarm_substrate import blackboard as bbx
    bb = _isolate(monkeypatch, tmp_path)
    cid = "run-retro"
    _seed_routing_board(cid)
    assert swarm_state.snapshot(cid, blackboard_dir=bb, opener=_no_conductor)["retrospective"] is None
    bbx.post_fact(cid, "retrospective", "retrospective", {
        "summary": "st-lru was refused store.go", "learnings": [
            {"scope": "process", "text": "slice write roots to the criteria"},
            {"scope": "repo", "text": "Stats lives in store/stats.go"}]})
    snap = swarm_state.snapshot(cid, blackboard_dir=bb, opener=_no_conductor)
    assert snap["retrospective"]["summary"] == "st-lru was refused store.go"
    assert snap["retrospective"]["recorded"] == 2 and snap["retrospective"]["learnings"][1]["scope"] == "repo"
    assert "retrospective recorded 2 learning(s)" in snap["summary"]
