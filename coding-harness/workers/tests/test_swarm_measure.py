"""swarm_measure: one measurement per code state (run 2bc5d0c6 ran the whole-app check 18 times in a round)."""
from __future__ import annotations

import json
import os

from common import git
from swarm.tasks import swarm_measure


def _repo(tmp_path):
    repo = str(tmp_path / "clone")
    git.ensure_ready(repo)
    os.makedirs(os.path.join(repo, ".cc"), exist_ok=True)
    counter = tmp_path / "runs.txt"
    with open(os.path.join(repo, ".cc", "count.cjs"), "w") as fh:
        fh.write(f"require('node:fs').appendFileSync({str(counter)!r}, 'x'); console.log('ok')\n")
    with open(os.path.join(repo, ".cc", "checks.json"), "w") as fh:
        json.dump([{"id": "count", "command": "node .cc/count.cjs"}], fh)
    return repo, counter


def test_the_tree_is_committed_measured_once_and_reused_until_it_changes(tmp_path, fake_task_input):
    repo, counter = _repo(tmp_path)
    first = swarm_measure(fake_task_input(workdir=repo, label="swarm round 1")).output_data
    assert first["allGreen"] is True and first["reused"] is False and counter.read_text() == "x"
    assert ".cc/checks.json" in first["changes"]  # what the commit changed, for the architect's review
    assert git.git(repo, "status", "--porcelain").stdout.strip() == ""  # the round's work is committed
    again = swarm_measure(fake_task_input(workdir=repo, label="review", previous=first)).output_data
    assert again["reused"] is True and again["tree"] == first["tree"] and counter.read_text() == "x"
    with open(os.path.join(repo, "app.txt"), "w") as fh:  # the architect changed something
        fh.write("fix\n")
    changed = swarm_measure(fake_task_input(workdir=repo, label="review", previous=first)).output_data
    assert changed["reused"] is False and changed["tree"] != first["tree"] and counter.read_text() == "xx"
    assert "review" in git.git(repo, "log", "-1", "--format=%s").stdout


def test_the_architects_verdict_settles_items_it_did_not_hand_back(tmp_path, monkeypatch, fake_task_input):
    """Run dc50a7bb: the launcher agent reported its item unfinished, the architect completed it in review
    and delivered, and the item stayed "reopened" on a delivered run's board."""
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import team_board as tb
    from swarm.tasks import swarm_round
    run = "run-settle"
    for iid in ("api", "launcher", "ui"):
        tb.post_item(run, "architect", {"id": iid, "title": iid})
    tb.set_state(run, "agent", "api", tb.REVIEW)
    tb.set_state(run, "agent", "launcher", tb.REOPENED)
    tb.set_state(run, "agent", "ui", tb.REOPENED)
    out = swarm_round(fake_task_input(runId=run, allGreen=False, evaluation={"structured": {
        "delivered": False, "assessment": "ui still red", "items": [{"id": "ui", "title": "ui", "brief": "fix"}]}})).output_data
    items = tb.fold_board(run)["items"]
    assert out["delivered"] is False
    assert (items["api"]["state"], items["launcher"]["state"], items["ui"]["state"]) == (tb.DONE, tb.DONE, tb.REOPENED)


def test_the_round_review_starts_where_round_reviews_delivered_and_climbs_per_undelivered_round(
        tmp_path, monkeypatch, fake_task_input):
    """Run 649fa3c8: round reviews on the cheapest rung handed back "verify" items twice before the
    strongest fixed the integration; the round review's rung is learned across runs."""
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from swarm.tasks import swarm_round
    resolution = {"roles": {"review": {"tiers": [{"model": "terra"}, {"model": "terra-high"}, {"model": "sol"}]}}}
    fold = lambda rnd: swarm_round(fake_task_input(runId="r1", round=rnd, modelResolution=resolution)).output_data
    assert fold(1)["roundReviewRung"] == 0 and fold(2)["roundReviewRung"] == 1  # no history: cheapest, then climb
    decide = lambda run, rung, delivered: swarm_round(fake_task_input(
        runId=run, round=1, roundReviewRung=rung, allGreen=True,
        evaluation={"structured": {"delivered": delivered, "assessment": "", "items": []}}))
    decide("r1", 0, False)
    decide("r1", 1, False)
    decide("r1", 2, True)
    assert fold(1)["roundReviewRung"] == 2 and fold(3)["roundReviewRung"] == 2  # learned, and capped at the top
