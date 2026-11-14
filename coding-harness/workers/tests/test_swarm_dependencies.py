"""Swarm items with dependencies: each item is its own swarm_item sub-workflow, started the moment its
last dependency is accepted by its review; chains of any depth nest, a join starts exactly once."""
from __future__ import annotations

from swarm.tasks import CLAIM_KIND, _graph, swarm_dispatch, swarm_item_release


def _it(iid, *deps):
    return {"id": iid, "title": iid, "brief": iid, "kind": "module", "rung": 0, "dependsOn": list(deps)}


def test_the_graph_finds_roots_chains_joins_and_what_can_never_start():
    items = [_it("db"), _it("api", "db"), _it("ui", "api"), _it("e2e", "api", "ui"),
             _it("a", "b"), _it("b", "a"), _it("c", "a"), _it("ghost", "nope"), _it("old", "prev")]
    deps, unstartable = _graph(items, {"prev": {"state": "done"}})
    assert deps["e2e"] == ["api", "ui"] and deps["db"] == [] and deps["old"] == []
    assert "cycle" in unstartable["a"] and "cycle" in unstartable["b"]
    assert "cannot start" in unstartable["c"] and "not an item" in unstartable["ghost"]
    assert set(unstartable) == {"a", "b", "c", "ghost"}
    _, later = _graph([_it("x", "prev")], {"prev": {"state": "in_progress"}})
    assert "not done" in later["x"]


def _dispatch(fake_task_input, run, items, workdir):
    return swarm_dispatch(fake_task_input(items=items, round=1, runId=run, workdir=workdir, maxWidth=10)).output_data


def _release(fake_task_input, run, item, workdir, accepted):
    return swarm_item_release(fake_task_input(
        item=item, round=1, runId=run, workdir=workdir, rung=0, reviewRung=0,
        work={"status": "success", "structured": {"summary": "built", "done": True, "files": [f"{item['id']}.js"]}},
        review={"status": "success", "structured": {"accepted": accepted, "notes": "checked", "fixed": []}})).output_data


def test_dependents_start_once_their_last_dependency_is_accepted_and_a_rejection_holds_the_chain(
        tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import blackboard as bb
    from common import git
    from common import team_board as tb
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    run = "run-deps"
    items = [_it("db"), _it("auth"), _it("api", "db", "auth"), _it("ui", "api"), _it("docs", "ui")]
    out = _dispatch(fake_task_input, run, items, work)
    assert sorted(t["taskReferenceName"] for t in out["dynamicTasks"]) == ["item_auth_r1", "item_db_r1"]
    assert all(t["type"] == "SUB_WORKFLOW" and t["subWorkflowParam"]["name"] == "swarm_item" for t in out["dynamicTasks"])
    assert tb.fold_board(run)["items"]["api"]["state"] == tb.BACKLOG
    # db done: api still waits for auth.
    assert _release(fake_task_input, run, items[0], work, True)["dynamicTasks"] == []
    assert tb.fold_board(run)["items"]["db"]["state"] == tb.DONE  # accepted is done, not "in review"
    # auth done: api, whose last dependency this was, starts, once.
    started = _release(fake_task_input, run, items[1], work, True)["dynamicTasks"]
    assert [t["taskReferenceName"] for t in started] == ["item_api_r1"]
    assert _release(fake_task_input, run, items[1], work, True)["dynamicTasks"] == []  # never twice
    claims = [f["data"]["item"] for f in bb.read_facts(run, kind=CLAIM_KIND)]
    assert sorted(claims) == ["api", "auth", "db"]
    # api rejected by its review: ui and docs (two levels down) are held, not started.
    held = _release(fake_task_input, run, items[2], work, False)
    assert held["dynamicTasks"] == [] and sorted(held["held"]) == ["docs", "ui"]
    board = tb.fold_board(run)["items"]
    assert board["ui"]["state"] == tb.BLOCKED and "api" in board["ui"]["reason"] and board["api"]["state"] == tb.REOPENED
