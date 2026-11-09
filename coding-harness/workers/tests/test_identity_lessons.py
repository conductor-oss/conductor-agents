"""Closure plan WP7 (H16: identity, constraint lifecycle, pushback) and WP8 (H10/H15: real lessons,
retraction, symbol-level decay)."""
import os

import common  # noqa: F401
from common import actors, git, lesson_refs, monitor, repo_memory, team_board as tb
from team.tasks import team_apply_reviews, team_integrate, team_plan_init, team_standup


def _iso(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))


def test_authority_is_derived_from_a_registered_author_not_claimed():
    assert actors.resolve("test-token")["author"] == "human:ops@example.com"
    assert actors.resolve("bogus") is None
    ds = tb.with_authority([
        {"by": "human:ops@example.com", "authority": "human", "text": "a"},
        {"by": "human:mallory@example.com", "authority": "human", "text": "forged"},
        {"by": "human", "authority": "human", "text": "anonymous"},
        {"by": "monitor", "authority": "monitor", "text": "m"},
        {"by": "reviewer_x", "authority": "human", "text": "reviewer claiming human"},
    ])
    assert [d["authority"] for d in ds] == ["human", "agent", "agent", "monitor", "agent"]


def test_constraint_lifecycle_lift_supersede_expire():
    ds = [{"id": "1", "text": "keep", "ts": 1.0}, {"id": "2", "text": "lifted", "ts": 1.0},
          {"op": "lift", "lifts": "2", "ts": 2.0}, {"id": "3", "text": "old", "ts": 1.0},
          {"id": "4", "text": "new", "supersedes": "3", "ts": 3.0},
          {"id": "5", "text": "two-wave", "expiresAfterWaves": 2, "ts": 1.0}]
    assert [d["text"] for d in tb.active_directives(ds, wave_ts=[1.5, 2.5])] == ["keep", "new", "two-wave"]
    assert [d["text"] for d in tb.active_directives(ds, wave_ts=[1.5, 2.5, 3.5])] == ["keep", "new"]


def test_monitor_flags_over_constrained_items_never_lifts():
    board = {"items": {"a": {"id": "a", "state": "reopened", "attempt": 2, "files": []},
                       "b": {"id": "b", "state": "ready", "attempt": 0, "files": []}},
             "directives": [{"target": "a", "authority": "human", "text": "no regex"},
                            {"target": "b", "authority": "human", "text": "c1"},
                            {"target": "b", "authority": "human", "text": "c2"},
                            {"target": "b", "authority": "human", "text": "c3"}], "reviews": []}
    out = monitor.detect(board, max_human_constraints=3, stall_attempts=2)
    flagged = {e["item"] for e in out["exceptions"] if e["kind"] == "over_constrained"}
    assert flagged == {"a", "b"}  # a: failing while constrained; b: too many constraints
    assert not [a for a in out["autonomousActions"] if a.get("op") == "lift"]


def _shared(tmp_path):
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "svc.py"), "w") as fh:
        fh.write("def handle(x):\n    return x\n")
    git.git(shared, "add", "-A")
    git.git(shared, "commit", "-m", "base")
    return shared, git._current_branch(shared)


def _fork(shared, branch, gid, path, text):
    wt = os.path.join(shared, git.WORKTREES, gid)
    git.git(shared, "worktree", "add", wt, "-b", git.group_branch(shared, gid), branch)
    with open(os.path.join(wt, path), "w") as fh:
        fh.write(text)
    git.git(wt, "add", "-A")
    git.git(wt, "commit", "-m", gid)


def test_pushback_is_decided_by_the_lead_and_lessons_are_opinions(monkeypatch, tmp_path, fake_task_input):
    """Humans are passive: an honored pushback never waits for one. The Lead exempts that item from the
    one directive it cannot follow (still in force for everyone else), records the decision as a resolved
    escalation, and re-dispatches the item without it."""
    _iso(monkeypatch, tmp_path)
    shared, branch = _shared(tmp_path)
    cid = "run-push"
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {"id": "p", "files": ["p.py"]}, {"id": "q", "files": ["q.py"]}]))
    from common import blackboard as bbx
    tb.post_directive(cid, "human:ops@example.com", "team", "Do not use the stdlib json module", authority="human")
    d1 = tb.fold_board(cid)["directives"][-1]["id"]
    bbx.post_fact(cid, "lead", "dispatch", {"item": "p", "candidate": "p", "directives": [
        {"id": d1, "text": "Do not use the stdlib json module", "authority": "human"}]})
    bbx.post_fact(cid, "lead", "dispatch", {"item": "q", "candidate": "q", "directives": []})
    _fork(shared, branch, "p", "p.py", "P = 1\n")
    _fork(shared, branch, "q", "q.py", "Q = 1\n")
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="p,q",
        dispatched=[{"id": "p", "files": ["p.py"]}, {"id": "q", "files": ["q.py"]}],
        joined={"p__1": {"output": {"structured": {"summary": "s", "pushback": {
                    "directive": "no stdlib json", "reason": "parsing JSON without it breaks unicode escapes"}}}},
                "q__1": {"output": {"structured": {"summary": "s", "lessons": [
                    {"text": "svc.handle is the single entrypoint", "refs": ["svc.py::handle"]}]}}},
                "x__1": {"output": {"structured": {"summary": "s", "pushback": {"directive": "", "reason": ""}}}}},
    )).output_data
    assert out["pushbacks"][0]["item"] == "p" and out["reviewing"] == ["q"] and out["lessonsShared"] == 1
    assert out["pushbacks"][0]["directiveId"] == d1 and [r["id"] for r in out["retry"]] == ["p"]
    board = tb.fold_board(cid)
    assert board["items"]["p"]["state"] == tb.REOPENED and "exempted" in board["items"]["p"]["reason"]
    assert not tb.open_exceptions(board["exceptions"])  # decided, not waiting
    live = {d["id"]: d for d in tb.active_directives(board["directives"])}
    assert live[d1]["exempt"] == ["p"]  # still in force for every other item
    les = [x for x in board["lessons"] if x.get("provenance") == "implementer:q"][0]
    assert les["kind"] == "opinion" and les["confidence"] == "medium"
    # The re-dispatch omits the exempted directive; the digest labels the lesson an opinion; every
    # implementer is told how to push back.
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid)).output_data
    p = su["dynamicTasksInput"]["p"]["prompt"]
    assert "[opinion, medium] svc.handle is the single entrypoint" in p and "PUSHBACK" in p
    assert "stdlib json" not in p
    assert su["dynamicTasksInput"]["p"]["outputSchema"]["properties"]["pushback"]


def test_reviewer_retraction_and_in_run_decay(monkeypatch, tmp_path, fake_task_input):
    _iso(monkeypatch, tmp_path)
    shared, branch = _shared(tmp_path)
    cid = "run-retract"
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[{"id": "a", "files": ["a.py"]}]))
    tb.post_lesson(cid, "implementer:z", "auth uses JWT", kind="opinion", confidence="high")
    tb.post_lesson(cid, "implementer:z", "svc.handle validates input", kind="opinion", refs=["svc.py::handle"])
    tb.post_lesson(cid, "implementer:z", "legacy.py holds the parser", kind="opinion", refs=["legacy.py"])
    assert [x["confidence"] for x in tb.fold_board(cid)["lessons"]][0] == "medium"  # opinion capped
    tb.set_state(cid, "i", "a", tb.REVIEW)
    out = team_apply_reviews(fake_task_input(correlationId=cid, reviewed=["a"], joined={"review_a__1": {"output": {
        "structured": {"verdict": "lgtm", "findings": [], "retract": ["auth uses JWT"]}}}})).output_data
    assert out["retracted"] == ["auth uses JWT"]
    live = [x["text"] for x in tb.live_lessons(tb.fold_board(cid)["lessons"], repo=shared)]
    assert "auth uses JWT" not in live                 # retracted swarm-wide
    assert "svc.handle validates input" in live        # symbol still there
    assert "legacy.py holds the parser" not in live    # file gone -> decayed in-run
    with open(os.path.join(shared, "svc.py"), "w") as fh:
        fh.write("def dispatch(x):\n    return x\n")  # rename the symbol; the file survives
    live = [x["text"] for x in tb.live_lessons(tb.fold_board(cid)["lessons"], repo=shared)]
    assert "svc.handle validates input" not in live


def test_repo_memory_symbol_decay_catches_a_rename_in_a_surviving_file(tmp_path):
    repo = str(tmp_path)
    with open(os.path.join(repo, "service.py"), "w") as fh:
        fh.write("def handle(req):\n    pass\n")
    mem = repo_memory.merge_lessons({}, [{"text": "handle() is the entrypoint", "refs": ["service.py::handle"]},
                                         {"text": "service.py exists", "refs": ["service.py"]}])
    assert repo_memory.decay_lessons(mem, repo)["invalidated"] == 0
    with open(os.path.join(repo, "service.py"), "w") as fh:
        fh.write("def dispatch(req):\n    pass\n")
    out = repo_memory.decay_lessons(mem, repo)
    assert out["invalidated"] == 1
    assert [x["text"] for x in repo_memory.live_lessons(out["memory"])] == ["service.py exists"]
    assert lesson_refs.verify(repo, "service.py::dispatch") and not lesson_refs.verify(repo, "service.py::disp")



def test_pushback_must_name_a_directive_the_agent_was_given():
    from team.tasks import _honored_pushback
    given = ["Do not import datetime, time, or any module that reads the clock."]
    assert _honored_pushback({"directive": "do not import datetime or time", "reason": "now_iso needs a clock"}, given)
    # Live-found (run 484c7a00): 'I could not run make test' is not a refused constraint.
    assert _honored_pushback({"directive": "Run the test suite with make test", "reason": "Bash denied"}, given) is None
    assert _honored_pushback({"directive": "anything", "reason": "x"}, []) is None
    assert _honored_pushback({"directive": "", "reason": ""}, given) is None
