"""The round's time sinks of run b74d3da7: a session kept alive after its result, a boot break found only after
the round, an item chain, a review fixing everything itself."""
from __future__ import annotations

import asyncio

import pytest
import time

from common import coding_agent, git
from swarm import team
from swarm.tasks import SMOKE_KIND, swarm_dispatch, swarm_item_release


def test_a_session_ends_soon_after_its_result_is_accepted(monkeypatch):
    monkeypatch.setattr(coding_agent, "RESULT_GRACE_S", 0.2)

    async def stream():
        yield "result handed in"
        await asyncio.sleep(30)  # the CLI waiting on a command it moved to the background
        yield "never seen"
    done = {"at": None}

    async def drive():
        seen = []
        async for m in coding_agent._until_result(stream(), done):
            seen.append(m)
            done["at"] = time.monotonic()
        return seen
    started = time.monotonic()
    assert asyncio.run(drive()) == ["result handed in"] and time.monotonic() - started < 5


def test_chains_longer_than_two_are_a_plan_problem():
    items = [{"id": "core", "dependsOn": []}, {"id": "eval", "dependsOn": ["core"]}, {"id": "fn", "dependsOn": ["eval"]},
             {"id": "pair", "dependsOn": ["core"]}]
    problems = team.chain_problems(items)
    assert len(problems) == 1 and "core -> eval -> fn" in problems[0]
    assert team.chain_problems(items[:2] + items[3:]) == []


def _it(iid, *deps):
    return {"id": iid, "title": iid, "brief": iid, "kind": "api", "rung": 0, "dependsOn": list(deps)}


def _release(fake_task_input, run, work, item):
    return swarm_item_release(fake_task_input(
        item=item, round=1, runId=run, workdir=work, rung=0, maxRung=2, reviewRung=0,
        work={"status": "success", "structured": {"summary": "built", "done": True, "files": ["x.go"]}},
        review={"status": "success", "structured": {"accepted": True, "notes": "fine", "fixed": []}})).output_data


def test_an_item_that_breaks_a_working_boot_is_sent_back(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from swarm import tasks
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    run = "run-smoke"
    swarm_dispatch(fake_task_input(items=[_it("a"), _it("b"), _it("c")], round=1, runId=run, workdir=work, maxWidth=10))
    from common import blackboard as bb
    bb.post_fact(run, "harness", SMOKE_KIND, {"round": 1, "item": "earlier", "ok": True, "output": ""})  # it started
    results = iter([{"ok": True, "output": ""}, {"ok": False, "output": 'duplicate command "setbit"'}])
    monkeypatch.setattr(tasks, "_run_smoke", lambda w, since=0.0: next(results))
    assert _release(fake_task_input, run, work, _it("a"))["accepted"] is True
    broke = _release(fake_task_input, run, work, _it("b"))
    # It broke a boot that worked: back to an agent at once, a rung up, with the boot failure.
    (ref, inp), = broke["dynamicTasksInput"].items()
    assert broke["accepted"] is False and ref == "item_b_r1_rework1"
    assert inp["item"]["rung"] == 1 and 'duplicate command "setbit"' in inp["item"]["brief"]


def test_with_no_culprit_one_fixer_starts_once_the_round_is_in(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import blackboard as bb
    from swarm import tasks
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    run = "run-fixer"
    swarm_dispatch(fake_task_input(items=[_it("a"), _it("b")], round=1, runId=run, workdir=work, maxWidth=10))
    ran = []
    monkeypatch.setattr(tasks, "_run_smoke", lambda w, since=0.0: ran.append(1) or {"ok": False, "output": 'duplicate command "setbit"'})
    first = _release(fake_task_input, run, work, _it("a"))
    assert first["accepted"] is True and first["dynamicTasks"] == []  # never started yet, b still building: nobody blamed
    assert ran == []  # and no smoke run: it would prove nothing about a
    last = _release(fake_task_input, run, work, _it("b"))
    (ref, inp), = last["dynamicTasksInput"].items()
    assert ref.startswith("item_smoke-fix-r1-1") and 'duplicate command "setbit"' in inp["item"]["brief"]
    assert [f["data"]["ok"] for f in bb.read_facts(run, kind=SMOKE_KIND)] == [False]  # one run, when the round was in


def test_a_session_at_its_turn_limit_hands_in_its_result(monkeypatch):
    calls = []

    async def fake_drive(prompt, options, on_turn=None):
        calls.append((prompt, getattr(options, "resume", None), getattr(options, "max_turns", None)))
        return {"status": "success", "structured": {"delivered": False, "items": [{"id": "fix-save"}]},
                "num_turns": 2, "cost_usd": 0.1, "turns": [], "turn_log": [], "session_id": "s1"}
    monkeypatch.setattr(coding_agent, "_drive", fake_drive)
    import claude_agent_sdk as sdk
    first = {"status": "error_max_turns", "structured": None, "session_id": "s1", "num_turns": 26, "cost_usd": 1.5,
             "turns": [], "turn_log": []}
    res = asyncio.run(coding_agent._hand_in_after_turn_limit(first, sdk.ClaudeAgentOptions(max_turns=25)))
    assert res["status"] == "success" and res["structured"]["items"][0]["id"] == "fix-save"
    assert res["num_turns"] == 28 and abs(res["cost_usd"] - 1.6) < 1e-9
    assert calls == [(coding_agent.HAND_IN_PROMPT, "s1", coding_agent.HAND_IN_TURNS)]  # the same session, resumed


def test_releases_waiting_on_the_smoke_lock_share_one_run(tmp_path):
    """A run that started after an item's change landed answers for it: no queue of one run per release."""
    import json
    import sys
    import time
    from swarm import tasks
    work = tmp_path / "w"
    (work / ".cc").mkdir(parents=True)
    count = work / "count.txt"
    (work / ".cc" / "checks.json").write_text(json.dumps([{"id": "server-startup", "smoke": True, "command":
        f"{sys.executable} -c \"open('count.txt','a').write('x')\""}]))
    landed = time.time()
    first = tasks._run_smoke(str(work), since=landed)
    again = tasks._run_smoke(str(work), since=landed)  # this item landed before that run started: shared
    later = tasks._run_smoke(str(work), since=time.time() + 1)  # landed after it: runs again
    assert first["ok"] is True and again.get("shared") is True and not later.get("shared")
    assert count.read_text() == "xx"


def test_at_most_two_smoke_checks_and_the_boot_check_first(tmp_path):
    import json
    from swarm import tasks
    work = tmp_path / "w"
    (work / ".cc").mkdir(parents=True)
    checks = [{"id": f"feature-{n}", "smoke": True, "command": "true"} for n in range(5)]
    checks.append({"id": "server-startup", "smoke": True, "command": "true"})
    (work / ".cc" / "checks.json").write_text(json.dumps(checks))
    assert [c["id"] for c in tasks._smoke_checks(str(work))] == ["server-startup", "feature-0"]


def test_the_width_is_items_at_once_and_the_rest_start_as_slots_free(tmp_path, monkeypatch, fake_task_input):
    """maxWidth limits how many items run at once; it never drops items (run 09a229e8 lost 24 of 35)."""
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from swarm import tasks
    monkeypatch.setattr(tasks, "_run_smoke", lambda w, since=0.0: {"ok": None, "output": ""})
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    run = "run-width"
    plan = [_it("leaf-1"), _it("leaf-2"), _it("leaf-3"), _it("base"), _it("uses-base", "base")]
    out = swarm_dispatch(fake_task_input(items=plan, round=1, runId=run, workdir=work, maxWidth=2)).output_data
    started = [t["taskReferenceName"] for t in out["dynamicTasks"]]
    assert len(started) == 2 and started[0].startswith("item_base_")  # what others wait on goes first
    nxt = _release(fake_task_input, run, work, _it("base"))
    names = [t["taskReferenceName"] for t in nxt["dynamicTasks"]]
    assert len(names) == 1 and names[0].startswith("item_uses-base_")  # its slot goes to its dependent
    rest = []
    for iid in ("leaf-1", "uses-base"):
        rest += [t["taskReferenceName"] for t in _release(fake_task_input, run, work, _it(iid, *(["base"] if iid == "uses-base" else [])))["dynamicTasks"]]
    assert sorted(r.split("_r1")[0] for r in rest) == ["item_leaf-2", "item_leaf-3"]  # nothing dropped


def test_ids_sharing_their_first_40_characters_get_different_task_names():
    from swarm import tasks
    a = "primary-dashboard-design-primary-dashboard-filters-check"
    b = "primary-dashboard-design-primary-dashboard-summary-check"
    assert tasks._ref(a) != tasks._ref(b) and len(tasks._ref(a)) <= 40
    assert tasks._ref("short-id") == "short-id"  # short ids keep their names


def test_a_continuation_resumes_from_the_phase_the_earlier_run_stopped_in(tmp_path):
    import json
    from swarm import tasks
    work = tmp_path / "w"
    work.mkdir()
    follow = {"continueFrom": "run-1"}
    assert tasks._resume_phase(str(work), {}) == "new"  # a new request starts at the beginning
    assert tasks._resume_phase(str(work), follow) == "design"  # stopped in design: design continues
    (work / "design").mkdir()
    (work / "design" / "architecture.md").write_text("x")
    assert tasks._resume_phase(str(work), follow) == "plan"  # design done: the lead plans from it
    (work / ".cc").mkdir()
    (work / ".cc" / "checks.json").write_text(json.dumps([{"id": "a", "command": "true"}]))
    assert tasks._resume_phase(str(work), follow) == "build"  # planned: straight to the rounds
    assert tasks._resume_phase(str(work), {"description": "Follow-up to an earlier swarm run (\"x\")."}) == "build"


def test_no_session_command_runs_in_the_background_or_past_its_limit(tmp_path, monkeypatch):
    """Run 2678a14f: the CLI moved a verifier's hung socket script into the background and the session sat 95 minutes."""
    import asyncio
    from common import git
    work = str(tmp_path / "w")
    git.ensure_ready(work)
    seen = {}

    class Stop(Exception):
        pass

    def capture(**opts):
        seen.update(opts)
        raise Stop
    monkeypatch.setattr(coding_agent.sdk, "ClaudeAgentOptions", capture)
    with pytest.raises(Stop):
        asyncio.run(coding_agent.run_coding_agent("hi", worktree=work))
    env = seen["env"]
    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert int(env["BASH_DEFAULT_TIMEOUT_MS"]) == 300000 and int(env["BASH_MAX_TIMEOUT_MS"]) == 600000
