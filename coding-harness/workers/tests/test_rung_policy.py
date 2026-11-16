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


# The swarm's adaptive loop (2026-11-16): a build item its review does not accept climbs a rung at once, inside
# its round; the implementer's rung never starts below what history shows works; a reviewer whose accepted
# item comes back under a new id learns from it.

_LADDER = {"roles": {"code": {"tiers": [{"model": "s", "effort": "medium"}, {"model": "s", "effort": "high"},
                                        {"model": "o", "effort": "high"}]}}}


def _release(fake_task_input, work, item, *, rung, status="success", accepted=False, notes="the PING reply is wrong"):
    from swarm.tasks import swarm_item_release
    return swarm_item_release(fake_task_input(
        item=item, round=1, runId="run-climb", workdir=work, rung=rung, maxRung=2, reviewRung=0,
        work={"status": status, "error": "stream broke" if status != "success" else None,
              "structured": {"summary": "built", "done": True, "files": ["a.go"]} if status == "success" else None},
        review={"status": "success", "structured": {"accepted": accepted, "notes": notes, "fixed": []}})).output_data


def test_a_rejected_build_item_climbs_a_rung_at_once_and_stops_after_two_reworks(tmp_path, monkeypatch,
                                                                                 fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import git, team_board
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    item = {"id": "ping", "title": "PING", "brief": "reply PONG", "kind": "api", "rung": 0, "dependsOn": []}
    (ref, inp), = _release(fake_task_input, work, item, rung=0)["dynamicTasksInput"].items()
    assert ref.endswith("_rework1") and inp["item"]["rung"] == 1 and inp["item"]["attempt"] == 2
    assert "the PING reply is wrong" in inp["item"]["brief"]
    assert team_board.fold_board("run-climb")["items"]["ping"]["state"] == team_board.IN_PROGRESS
    (ref2, inp2), = _release(fake_task_input, work, inp["item"], rung=1)["dynamicTasksInput"].items()
    assert ref2.endswith("_rework2") and inp2["item"]["rung"] == 2
    assert inp2["item"]["brief"].count("YOUR LAST ATTEMPT WAS NOT ACCEPTED") == 1
    assert _release(fake_task_input, work, inp2["item"], rung=2)["dynamicTasks"] == []  # the round review has it


def test_a_session_that_never_ran_retries_on_the_same_rung(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import git
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    item = {"id": "ping", "title": "PING", "brief": "b", "kind": "api", "rung": 1, "dependsOn": []}
    (_, inp), = _release(fake_task_input, work, item, rung=1, status="sdk_error")["dynamicTasksInput"].items()
    assert inp["item"]["rung"] == 1 and "agent_unavailable" in inp["item"]["brief"]


def test_the_implementer_starts_no_lower_than_history_shows_works(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import git
    from swarm.tasks import swarm_item_prep
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    key = rp.repo_key(work)
    rp.record(key, role="code", kind="api", rung=0, ok=False, cause="not_accepted")
    rp.record(key, role="code", kind="ui", rung=0, ok=False, cause="agent_unavailable")  # not the agent's
    item = {"id": "ping", "title": "PING", "brief": "b", "kind": "api", "rung": 0, "dependsOn": []}
    out = swarm_item_prep(fake_task_input(item=item, round=1, runId="r", workdir=work, repoPath=work,
                                          modelResolution=_LADDER)).output_data
    assert out["rung"] == 1 and "raised from the architect's rung 0" in out["rungReason"]
    ui = swarm_item_prep(fake_task_input(item={**item, "kind": "ui"}, round=1, runId="r", workdir=work, repoPath=work,
                                         modelResolution=_LADDER)).output_data
    assert ui["rung"] == 0
    high = swarm_item_prep(fake_task_input(item={**item, "rung": 2}, round=1, runId="r", workdir=work, repoPath=work,
                                           modelResolution=_LADDER)).output_data
    assert high["rung"] == 2  # the architect may start it higher


def test_a_reviewer_whose_accepted_item_is_replaced_learns_it_missed(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import blackboard as bb, git
    from swarm.tasks import REPORT_KIND, swarm_round
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    for iid in ("ping", "echo"):
        bb.post_fact("run-rv", "harness", REPORT_KIND, {"round": 1, "item": iid, "kind": "api", "rung": 0,
                                                        "reviewRung": 0, "accepted": True, "reviewAccepted": True,
                                                        "done": True})
    swarm_round(fake_task_input(runId="run-rv", round=1, workdir=work, repoPath=work, allGreen=False,
                                roundReviewRung=0,
                                evaluation={"structured": {"delivered": False, "assessment": "PING is wrong",
                                                           "items": [{"id": "fix-ping", "title": "t", "brief": "b",
                                                                      "kind": "api", "rung": 1, "dependsOn": [],
                                                                      "replaces": ["ping"]}]}}))
    rows = {r["item"]: r["ok"] for r in rp.history(rp.repo_key(work), role="review", kind="api")}
    assert rows == {"ping": False, "echo": True}


def test_an_item_its_agent_did_not_finish_is_reworked_even_when_its_review_accepted(tmp_path, monkeypatch,
                                                                                    fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import git
    from swarm.tasks import swarm_item_release
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    item = {"id": "render", "title": "render", "brief": "b", "kind": "report", "rung": 0, "dependsOn": []}

    def release(structured):
        return swarm_item_release(fake_task_input(
            item=item, round=1, runId="run-nd", workdir=work, rung=0, maxRung=2, reviewRung=0,
            work={"status": "success", "structured": structured},
            review={"status": "success", "structured": {"accepted": True, "notes": "reads fine", "fixed": []}})).output_data

    out = release({"summary": "wrote render.py", "done": False, "files": ["render.py"],
                   "blockers": ["fonts.py is not on disk yet"]})
    (_, inp), = out["dynamicTasksInput"].items()
    assert out["accepted"] is False and inp["item"]["rung"] == 1 and "fonts.py is not on disk yet" in inp["item"]["brief"]
    # Refused by the sandbox: an approver decides; no rework and no climb.
    held = release({"summary": "fonts not vendored", "done": False, "files": [],
                    "permissions": [{"kind": "network", "target": "github.com", "why": "DejaVu fonts"}]})
    assert held["accepted"] is False and held["dynamicTasks"] == []


def test_an_item_waiting_on_a_sibling_restarts_on_the_same_rung_when_the_sibling_lands(tmp_path, monkeypatch,
                                                                                       fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import git, team_board
    from swarm.tasks import swarm_dispatch, swarm_item_release
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    run = "run-wait"
    items = [{"id": i, "title": i, "brief": i, "kind": "api", "rung": 0, "dependsOn": []}
             for i in ("core-server", "pubsub-cmds")]
    swarm_dispatch(fake_task_input(items=items, round=1, runId=run, workdir=work, maxWidth=10))

    def release(item, structured, accepted):
        return swarm_item_release(fake_task_input(
            item=item, round=1, runId=run, workdir=work, rung=1, maxRung=2, reviewRung=0,
            work={"status": "success", "structured": structured},
            review={"status": "success", "structured": {"accepted": accepted, "notes": "n", "fixed": []}})).output_data

    waiting = release(items[1], {"summary": "pubsub.go written", "done": False, "files": ["pubsub.go"],
                                 "blockers": ["internal/server does not compile until core-server lands NotifyKey"]}, True)
    assert waiting["dynamicTasks"] == []  # no climb, no retry yet: it waits
    assert team_board.fold_board(run)["items"]["pubsub-cmds"]["state"] == team_board.BLOCKED
    landed = release(items[0], {"summary": "server", "done": True, "files": ["server.go"]}, True)
    (ref, inp), = landed["dynamicTasksInput"].items()
    assert ref.endswith("_wake2") and inp["item"]["rung"] == 1 and "WHAT YOU WAITED FOR HAS LANDED (core-server" in inp["item"]["brief"]
    assert release(items[0], {"summary": "server", "done": True, "files": ["server.go"]}, True)["dynamicTasks"] == []  # once
    from common import blackboard as bb
    from swarm.tasks import REPORT_KIND
    first = [d["data"] for d in bb.read_facts(run, kind=REPORT_KIND) if d["data"]["item"] == "pubsub-cmds"][0]
    assert first["cause"] == "waiting"  # the ladder does not count it against rung 1
