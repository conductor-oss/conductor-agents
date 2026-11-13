"""Hill climbing over the model ladder (common/rung_policy) and its wiring into the team.

The user's ask (2026-11-11): sub-agents have well-scoped work, so they should not always run the
biggest model at the highest thinking; they climb when they get stuck and the system adapts. Run
a87e9ddb ran 32 sessions on Opus at high effort because a harness fault looked like a stuck agent.
"""

from __future__ import annotations

import pytest

from common import rung_policy as rp


def _row(rung, ok, ts, cause=""):
    return {"rung": rung, "ok": ok, "ts": ts, "cause": cause}


def test_first_item_of_a_kind_starts_on_the_cheapest_rung():
    assert rp.choose_start([], top=2)[0] == 0


def test_after_a_success_the_next_item_probes_one_rung_cheaper():
    rung, why = rp.choose_start([_row(2, False, 1), _row(1, True, 2)], top=2)
    assert rung == 0 and "probe rung 0" in why


def test_a_rung_that_failed_is_not_retried_until_the_rung_above_keeps_winning():
    rows = [_row(1, True, 1), _row(0, False, 2), _row(1, True, 3)]
    assert rp.choose_start(rows, top=2) == (1, "rung 1 is the cheapest that got this kind of item done (rung 0 failed)")
    rows += [_row(1, True, 4), _row(1, True, 5)]
    rung, why = rp.choose_start(rows, top=2)
    assert rung == 0 and "probe rung 0 again" in why


def test_when_nothing_has_worked_the_next_item_starts_above_the_highest_failure():
    assert rp.choose_start([_row(0, False, 1), _row(1, False, 2)], top=2)[0] == 2
    assert rp.choose_start([_row(2, False, 1)], top=2)[0] == 2


def test_only_agent_fixable_failures_count_against_a_rung():
    dispatches = [{"rung": 0, "ts": 1}, {"rung": 0, "ts": 3}, {"rung": 1, "ts": 5}]
    rescues = [{"cause": "agent_unavailable", "ts": 2}, {"cause": "review_rounds_exhausted", "ts": 4}]
    out = rp.outcomes_from_board(dispatches=dispatches, rescues=rescues, final_state="done")
    assert out == [{"rung": 0, "ok": False, "cause": "review_rounds_exhausted"}, {"rung": 1, "ok": True, "cause": ""}]
    assert rp.outcomes_from_board(dispatches=[{"rung": 2, "ts": 1}], rescues=[], final_state="blocked",
                                  blocked_reason="staging_fault") == []


@pytest.mark.parametrize("entry,kind", [
    ({"id": "readme", "deliverable": "docs", "files": ["README.md"]}, "docs"),
    ({"id": "be-scaffold", "files": ["backend/pom.xml"]}, "scaffold"),
    ({"id": "fe-base", "files": ["frontend/package.json", "frontend/vite.config.js"]}, "scaffold"),
    ({"id": "server", "dependsOn": ["parser", "storage", "protocol"], "files": ["src/server.rs"]}, "wiring"),
    ({"id": "parser", "dependsOn": ["scaffold"], "files": ["src/parser.rs"]}, "module"),
])
def test_items_are_grouped_by_the_kind_of_work(entry, kind):
    assert rp.item_kind(entry) == kind


def test_outcomes_are_shared_by_every_run_of_one_repository(tmp_path, monkeypatch, tmp_git_repo):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import git
    wt = git.worktree_add(str(tmp_git_repo), "run-1")["worktreePath"]
    key = rp.repo_key(wt)
    assert key == rp.repo_key(str(tmp_git_repo))
    rp.record(key, role="code", kind="module", rung=1, ok=True, item="parser", run="r1")
    assert rp.start_rung(key, role="code", kind="module", top=2)[0] == 0
    assert rp.start_rung(key, role="code", kind="scaffold", top=2)[0] == 0
    rp.record(key, role="code", kind="module", rung=0, ok=False, cause="review_rounds_exhausted", item="lexer", run="r2")
    assert rp.start_rung(key, role="code", kind="module", top=2)[0] == 1


def test_the_team_records_what_its_items_teach_the_ladder(tmp_path, monkeypatch, tmp_git_repo):
    """team_outcome folds each item's dispatches and rescues into outcomes on the shared store."""
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import blackboard as bb
    from team import tasks as team
    cid = "team-x"
    bb.post_fact(cid, "lead", "dispatch", {"item": "api", "rung": 0, "kind": "module"})
    bb.post_fact(cid, "lead", team.RESCUE_KIND, {"item": "api", "cause": "integration_check_red"})
    bb.post_fact(cid, "lead", "dispatch", {"item": "api", "rung": 1, "kind": "module"})
    bb.post_fact(cid, "lead", "dispatch", {"item": "db", "rung": 0, "kind": "scaffold"})
    n = team._record_rung_outcomes(cid, str(tmp_git_repo), {"api": {"state": "done"}, "db": {"state": "done"}})
    assert n == 3
    key = rp.repo_key(str(tmp_git_repo))
    assert [(r["rung"], r["ok"]) for r in rp.history(key, role="code", kind="module")] == [(0, False), (1, True)]
    assert rp.start_rung(key, role="code", kind="module", top=2)[0] == 1
    assert rp.start_rung(key, role="code", kind="scaffold", top=2)[0] == 0


def test_a_stuck_helper_climbs_only_for_an_agent_fixable_cause(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from team import tasks as team
    assert team._rescue("t1", "api", "agent_unavailable: the session crashed", max_rescues=2)
    assert team._rescue("t1", "api", "review_rounds_exhausted: 3 rounds", max_rescues=2)
    causes = [h["cause"] for h in team._rescue_map("t1")["api"]]
    assert causes == ["agent_unavailable", "review_rounds_exhausted"]
    assert [rp.agent_fixable(c) for c in causes] == [False, True]


def test_the_standup_dispatches_each_item_on_its_learned_rung_and_a_dead_session_does_not_climb(
        tmp_path, monkeypatch, fake_task_input):
    """End to end through the standup: a kind of item that needed rung 1 last run starts there; an item
    with no history starts on rung 0; a session that never completed is retried on its rung, while a
    review-exhausted item climbs one."""
    import os
    from common import git
    from common import team_board as tb
    from team import tasks as team
    from team.tasks import team_plan_init, team_standup
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    git.git(shared, "add", "-A"); git.git(shared, "commit", "-m", "base")
    key = rp.repo_key(shared)
    # An earlier run: module items failed on rung 0 and were done on rung 1.
    rp.record(key, role="code", kind="module", rung=0, ok=False, cause="review_rounds_exhausted", run="old")
    rp.record(key, role="code", kind="module", rung=1, ok=True, run="old")
    cid = "team-rungs"
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {"id": "api", "description": "add api", "files": ["api.py"], "dependsOn": []},
        {"id": "docs", "description": "write docs", "files": ["README.md"], "deliverable": "docs"}]))
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, maxWidth=6)).output_data
    assert su["dynamicTasksInput"]["api"]["modelTier"] == 1
    assert su["dynamicTasksInput"]["docs"]["modelTier"] == 0
    from common import blackboard as bb
    sent = {d["data"]["item"]: d["data"] for d in bb.read_facts(cid, kind="dispatch")}
    assert "cheapest that got this kind of item done" in sent["api"]["rungReason"]
    assert sent["docs"]["kind"] == "docs" and "no history" in sent["docs"]["rungReason"]
    # A dead session: same rung. Review rounds exhausted: one rung up.
    team._rescue(cid, "docs", "agent_unavailable: the session crashed", max_rescues=2)
    tb.set_state(cid, "lead", "api", tb.REOPENED, attempt=0)
    team._rescue(cid, "api", "review_rounds_exhausted: 3 rounds", max_rescues=2)
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, maxWidth=6)).output_data
    assert su["dynamicTasksInput"]["docs"]["modelTier"] == 0
    assert su["dynamicTasksInput"]["api"]["modelTier"] == 2
