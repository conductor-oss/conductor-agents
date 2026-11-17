"""Phase 2 team cycle: the workers that wire the board read-model into the team_build workflow.

Covers the glue the pure ``test_team_board.py`` does not: the router (H9), the standup's
ownership-gated dispatch + live lesson/directive injection, the integrator's board transitions +
spend, mid-run peer review (build + apply: lgtm→done, changes→reopen+directive, bounded rounds), and
the honest DELIVERED/INCOMPLETE outcome folded from the board.
"""
import os


from team.tasks import (team_apply_reviews, team_integrate, team_outcome,
                        team_plan_init, team_review_build, team_route, team_standup)
from common import git, team_board as tb


def _isolate_board(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))


_ITEMS = [
    {"id": "alpha", "description": "add alpha", "files": ["registry.py", "alpha.py"],
     "acceptanceCriteria": ["alpha is registered"]},
    {"id": "beta", "description": "add beta", "files": ["registry.py", "beta.py"]},
    {"id": "gamma", "description": "add gamma", "files": ["gamma.py"]},
]


def _task(d, ref):
    """A task of the workflow by reference name, inside loops, switch cases and fork branches."""
    def walk(ts):
        for t in ts:
            if t.get("taskReferenceName") == ref:
                return t
            for k in ("loopOver", "defaultCase"):
                r = walk(t.get(k) or [])
                if r:
                    return r
            for b in (t.get("decisionCases") or {}).values():
                r = walk(b)
                if r:
                    return r
            for b in t.get("forkTasks") or []:
                r = walk(b)
                if r:
                    return r
    return walk(d["tasks"])


def _commit_all(repo, msg):
    git.git(repo, "add", "-A")
    git.git(repo, "commit", "-m", msg)


def _fork_worktree(shared, gid, shared_branch, *, edit=None):
    wt = os.path.join(shared, git.WORKTREES, gid)
    git.git(shared, "worktree", "add", wt, "-b", git.group_branch(shared, gid), shared_branch)
    if edit is not None:
        path, text = edit
        with open(os.path.join(wt, path), "w") as fh:
            fh.write(text)
        _commit_all(wt, f"{gid} change")
    return wt


# ─────────────────────────── routing (H9 graceful degradation) ─────────────────
def test_route_single_vs_team(fake_task_input):
    single = team_route(fake_task_input(subtasks=[_ITEMS[0]])).output_data
    assert single["mode"] == "single" and "add alpha" in single["instruction"]
    team = team_route(fake_task_input(subtasks=_ITEMS)).output_data
    assert team["mode"] == "team" and team["itemCount"] == 3
    # Forcing overrides the count-based default.
    assert team_route(fake_task_input(subtasks=_ITEMS, forceSingle=True)).output_data["mode"] == "single"
    assert team_route(fake_task_input(subtasks=[_ITEMS[0]], forceTeam=True)).output_data["mode"] == "team"


# ─────────────────────────── standup: gate + injection ─────────────────────────
def test_standup_sizes_gates_and_injects_board(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-standup"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS, contractPaths=["contract.py"]))
    # A teammate's lesson + a human directive already on the board.
    tb.post_lesson(cid, "mentor", "registry.py uses append-only markers", confidence="high", refs=["seed.py"])
    tb.post_directive(cid, "human", "alpha", "do not use eval()", authority="human")

    out = team_standup(fake_task_input(
        repoPath=shared, correlationId=cid, contractPaths=["contract.py"], maxWidth=6)).output_data
    # alpha+beta both need registry.py -> ownership gate serializes them; gamma is disjoint.
    ids = out["groupIds"].split(",")
    assert "gamma" in ids and len([x for x in ("alpha", "beta") if x in ids]) == 1
    assert out["readyCount"] == 3 and out["dispatchedCount"] == len(ids)
    # The live board + lesson + the item's human directive are injected into alpha's prompt.
    if "alpha" in out["dynamicTasksInput"]:
        p = out["dynamicTasksInput"]["alpha"]["prompt"]
        assert "append-only markers" in p and "do not use eval()" in p and "FROZEN CONTRACT" in p
    # Dispatched items are now in_progress on the board; files claimed.
    board = tb.fold_board(cid)
    assert any(board["items"][i]["state"] == tb.IN_PROGRESS for i in ids)


def test_standup_budget_halt_dispatches_nothing(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-halt"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS, contractPaths=[]))
    out = team_standup(fake_task_input(
        repoPath=shared, correlationId=cid, maxWidth=6, budgetState="halt")).output_data
    assert out["teamSize"] == 0 and out["dispatchedCount"] == 0


# ─────────────────────────── integrate: board transitions + spend ──────────────
def test_integrate_moves_clean_item_to_review_and_records_spend(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-int"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "registry.py"), "w") as fh:
        fh.write("H = {}\n")
    _commit_all(shared, "base")
    shared_branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS, contractPaths=["contract.py"]))
    tb.set_state(cid, "lead", "alpha", tb.IN_PROGRESS)
    from common import file_ownership
    file_ownership.claim(cid, "alpha", "registry.py")
    _fork_worktree(shared, "alpha", shared_branch, edit=("registry.py", "H = {'a': 1}\n"))

    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=shared_branch, correlationId=cid, contractPaths=["contract.py"],
        groupIds="alpha", dispatched=[{"id": "alpha", "files": ["registry.py"], "attempt": 0}],
        enablePeerReview=True,
        joined={"alpha": {"output": {"costUsd": 0.5, "tokenUsed": 100, "model": "m",
                                     "deniedWrites": ["beta.py"], "outsideScope": ["gamma.py"]}}})).output_data
    assert out["reviewing"] == ["alpha"] and out["big_bang_merge"] is False
    assert out["releasedFiles"] == 1 and out["spendRecorded"] == 1
    board = tb.fold_board(cid)
    assert board["items"]["alpha"]["state"] == tb.REVIEW
    from common import budget_gate
    assert budget_gate.read_spend(cid)["usd"] == 0.5
    # The files the guard refused are recorded on the item's session fact: the next standup's
    # evidence for widening the item's write scope (run fc6467b7).
    sessions = [f["data"] for f in file_ownership.bb.read_facts(cid, kind="session")]
    # ...including a file the commit left unstaged as outside the roots (written through Bash, run ba2491ff).
    assert sessions[-1]["item"] == "alpha" and sessions[-1]["deniedWrites"] == ["beta.py", "gamma.py"]


def test_integrate_empty_delivery_reopens_then_blocks_at_cap(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-empty"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    shared_branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[0]], contractPaths=["contract.py"]))
    _fork_worktree(shared, "alpha", shared_branch)  # no commit -> empty delivery

    item = {"id": "alpha", "files": ["registry.py"], "attempt": 0}
    out = team_integrate(fake_task_input(maxRescues=0, 
        sharedRepo=shared, sharedBranch=shared_branch, correlationId=cid, contractPaths=["contract.py"],
        groupIds="alpha", dispatched=[item], maxAttempts=2, joined={})).output_data
    assert out["reviewing"] == [] and len(out["retry"]) == 1 and out["retry"][0]["attempt"] == 1
    assert tb.fold_board(cid)["items"]["alpha"]["state"] == tb.REOPENED

    out2 = team_integrate(fake_task_input(maxRescues=0, 
        sharedRepo=shared, sharedBranch=shared_branch, correlationId=cid, contractPaths=["contract.py"],
        groupIds="alpha", dispatched=[{**item, "attempt": 1}], maxAttempts=2, joined={})).output_data
    assert out2["retry"] == [] and len(out2["blocked"]) == 1
    assert tb.fold_board(cid)["items"]["alpha"]["state"] == tb.BLOCKED


# ─────────────────────────── peer review: build + apply ────────────────────────
def test_review_criteria_hidden_from_implementer_shown_to_reviewer(monkeypatch, tmp_path, fake_task_input):
    # H8/H13: reviewCriteria are the reviewer's check — injected into the reviewer, never the implementer.
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-hidden"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    base = git.head(shared)
    item = {"id": "a", "description": "implement add", "files": ["a.py"],
            "acceptanceCriteria": ["add returns the sum"],
            "reviewCriteria": ["add must raise ValueError on string input"]}
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[item], contractPaths=[]))

    # Standup prompt (implementer) must carry the acceptance criterion but NOT the hidden review criterion.
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, maxWidth=2)).output_data
    prompt = su["dynamicTasksInput"]["a"]["prompt"]
    assert "add returns the sum" in prompt and "raise ValueError on string input" not in prompt

    # Reviewer prompt must carry BOTH (the hidden criterion is the check).
    with open(os.path.join(shared, "a.py"), "w") as fh:
        fh.write("def add(a, b):\n    return a + b\n")
    _commit_all(shared, "impl")
    head = git.head(shared)
    rb = team_review_build(fake_task_input(sharedRepo=shared, correlationId=cid, reviewing=["a"],
                                          waveBase=base, finalHead=head, maxReviewRounds=2)).output_data
    rprompt = rb["dynamicTasksInput"]["review_a"]["prompt"]
    assert "raise ValueError on string input" in rprompt and "add returns the sum" in rprompt


def test_review_build_reviews_every_item_in_review(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-rev"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "alpha.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    base = git.head(shared)
    with open(os.path.join(shared, "alpha.py"), "w") as fh:
        fh.write("x = 2\n")
    _commit_all(shared, "alpha change")
    head = git.head(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[0]], contractPaths=[]))

    out = team_review_build(fake_task_input(
        sharedRepo=shared, correlationId=cid, reviewing=["alpha"], waveBase=base, finalHead=head,
        maxReviewRounds=2)).output_data
    assert out["reviewed"] == ["alpha"]
    ref = out["dynamicTasks"][0]["taskReferenceName"]
    assert ref == "review_alpha" and out["dynamicTasks"][0]["name"] == "coding_agent"
    assert out["dynamicTasksInput"][ref]["tools"] == ["Read", "Grep", "Glob"]
    # The reviewer reads its own worktree pinned at the reviewed head — never the shared integration
    # worktree, where the agent runner's end-of-session reset undid a sibling team's merge (a062184f).
    review_wt = out["dynamicTasksInput"][ref]["worktreePath"]
    assert review_wt == os.path.join(shared, git.WORKTREES, "review-alpha") and git.head(review_wt) == head
    assert git.git(review_wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "HEAD"  # detached
    with open(os.path.join(shared, "alpha.py"), "w") as fh:
        fh.write("x = 3\n")  # a sibling lands meanwhile: the reviewer's tree does not move
    _commit_all(shared, "sibling change")
    assert git.head(review_wt) == head and git.head(shared) != head
    assert out["dynamicTasksInput"][ref]["schema"]["properties"]["verdict"]["enum"] == ["lgtm", "changes"]
    # The reviewer knows what the implementer may write, so a fix that needs another file is a named
    # finding for the Lead, never a repeated demand the implementer cannot satisfy (run fc6467b7).
    assert "may write only: registry.py, alpha.py" in out["dynamicTasksInput"][ref]["prompt"]

    # WP1.1: the round cap is enforced on the verdict (quarantine), never by skipping review — an item
    # in review is always reviewed, and a REVIEW item left over from a prior wave is picked up too.
    tb.post_review(cid, "r1", "alpha", "changes")
    tb.post_review(cid, "r2", "alpha", "changes")
    tb.set_state(cid, "i", "alpha", tb.REVIEW)
    out2 = team_review_build(fake_task_input(
        sharedRepo=shared, correlationId=cid, reviewing=[], waveBase=base, finalHead=head,
        maxReviewRounds=2)).output_data
    assert out2["reviewed"] == ["alpha"]
    # Applying the verdicts removes the reviewers' worktrees.
    team_apply_reviews(fake_task_input(sharedRepo=shared, correlationId=cid, reviewed=["alpha"], joined={
        "review_alpha__3": {"output": {"structured": {"verdict": "lgtm", "findings": []}}}}))
    assert not os.path.exists(review_wt)
    assert "review-alpha" not in git.git(shared, "worktree", "list", check=False).stdout


def test_apply_reviews_changes_reopens_with_directive_lgtm_done(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-apply"
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[0], _ITEMS[1]], contractPaths=[]))
    tb.set_state(cid, "i", "alpha", tb.REVIEW)
    tb.set_state(cid, "i", "beta", tb.REVIEW)

    out = team_apply_reviews(fake_task_input(
        correlationId=cid, reviewed=["alpha", "beta"], maxReviewRounds=2,
        joined={
            "review_alpha": {"output": {"verdict": "changes", "findings": ["unmet criterion 1"]}},
            "review_beta": {"output": {"verdict": "lgtm", "findings": []}},
        })).output_data
    assert out["changes"] == ["alpha"] and out["doneCount"] == 1
    board = tb.fold_board(cid)
    assert board["items"]["alpha"]["state"] == tb.REOPENED and board["items"]["beta"]["state"] == tb.DONE
    # The findings became a directive the next standup will inject for alpha.
    assert any(d["target"] == "alpha" and "unmet criterion 1" in d["text"] for d in board["directives"])
    assert not out["drained"]  # alpha reopened -> still open


def test_apply_reviews_parses_real_coding_agent_output_shape(monkeypatch, tmp_path, fake_task_input):
    # A real coding_agent returns the structured verdict under `result` (a JSON string) / `structured`,
    # NOT at the top level. apply_reviews must read that, not default every review to lgtm.
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-shape"
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[0], _ITEMS[1]], contractPaths=[]))
    tb.set_state(cid, "i", "alpha", tb.REVIEW)
    tb.set_state(cid, "i", "beta", tb.REVIEW)
    out = team_apply_reviews(fake_task_input(
        correlationId=cid, reviewed=["alpha", "beta"], maxReviewRounds=2,
        joined={
            # alpha: the exact live shape (result is a JSON string); changes must be honored.
            "review_alpha": {"output": {"result": '{"verdict":"changes","findings":["unmet criterion 1"]}',
                                        "structured": {"verdict": "changes", "findings": ["unmet criterion 1"]}}},
            "review_beta": {"output": {"result": '{"verdict":"lgtm","findings":[]}'}},
        })).output_data
    assert out["changes"] == ["alpha"] and out["doneCount"] == 1
    board = tb.fold_board(cid)
    assert board["items"]["alpha"]["state"] == tb.REOPENED and board["items"]["beta"]["state"] == tb.DONE


def test_apply_reviews_reads_do_while_suffixed_join_keys(monkeypatch, tmp_path, fake_task_input):
    # Inside the wave loop the review JOIN is keyed review_<id>__N, not review_<id>. apply_reviews must
    # still read the real verdict (a bare-ref lookup silently defaulted every review to lgtm, hiding a
    # real 'changes'). Regression for the live bug the hidden-criteria run exposed.
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-suffix"
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[0]], contractPaths=[]))
    tb.set_state(cid, "i", "alpha", tb.REVIEW)
    out = team_apply_reviews(fake_task_input(
        correlationId=cid, reviewed=["alpha"], maxReviewRounds=2,
        joined={"review_alpha__1": {"output": {"result": '{"verdict":"changes","findings":["missing guard"]}'}}}
    )).output_data
    assert out["changes"] == ["alpha"]
    assert tb.fold_board(cid)["items"]["alpha"]["state"] == tb.REOPENED


def test_apply_reviews_quarantines_past_round_cap_never_accepts(monkeypatch, tmp_path, fake_task_input):
    # WP1.1 (H2/H8): a 'changes' verdict past the cap QUARANTINES the item for a human; it is never done.
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-cap"
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[0]], contractPaths=[]))
    tb.set_state(cid, "i", "alpha", tb.REVIEW)
    out = team_apply_reviews(fake_task_input(maxRescues=0, 
        correlationId=cid, reviewed=["alpha"], maxReviewRounds=1,
        joined={"review_alpha": {"output": {"verdict": "changes", "findings": ["f1"]}}})).output_data
    assert out["changes"] == ["alpha"]  # first changes is absorbed (cap 1)
    tb.set_state(cid, "i", "alpha", tb.REVIEW)
    out = team_apply_reviews(fake_task_input(maxRescues=0, 
        correlationId=cid, reviewed=["alpha"], maxReviewRounds=1,
        joined={"review_alpha": {"output": {"verdict": "changes", "findings": ["still imperfect"]}}})).output_data
    board = tb.fold_board(cid)
    assert out["quarantined"] == ["alpha"] and out["doneCount"] == 0
    assert board["items"]["alpha"]["state"] == tb.BLOCKED
    assert board["items"]["alpha"]["reason"] == "review_rounds_exhausted"
    exc = tb.open_exceptions(board["exceptions"])
    assert [(e["item"], e["kind"]) for e in exc] == [("alpha", "review_cap")]
    assert "still imperfect" in exc[0]["detail"]


def test_apply_reviews_unavailable_is_not_lgtm(monkeypatch, tmp_path, fake_task_input):
    # WP1.1: a reviewer that produced no verdict keeps the item in review, then quarantines it.
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-unavail"
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[0]], contractPaths=[]))
    tb.set_state(cid, "i", "alpha", tb.REVIEW)
    out = team_apply_reviews(fake_task_input(maxRescues=0, correlationId=cid, reviewed=["alpha"], maxReviewAttempts=2,
                                             joined={})).output_data
    assert out["unavailable"] == ["alpha"] and out["doneCount"] == 0
    assert tb.fold_board(cid)["items"]["alpha"]["state"] == tb.REVIEW
    out = team_apply_reviews(fake_task_input(maxRescues=0, correlationId=cid, reviewed=["alpha"], maxReviewAttempts=2,
                                             joined={"review_alpha": {"output": {"result": "free text"}}})).output_data
    board = tb.fold_board(cid)
    assert out["quarantined"] == ["alpha"] and board["items"]["alpha"]["reason"] == "reviewer_unavailable"
    assert any(e["kind"] == "reviewer_unavailable" for e in tb.open_exceptions(board["exceptions"]))


def test_reviewer_and_planner_spend_recorded(monkeypatch, tmp_path, fake_task_input):
    # WP1.2 (H5): reviewer and planner spend reach the run budget, reported by role.
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-spend"
    from common import budget_gate
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[0]], contractPaths=[],
                                   planUsage={"costUsd": 0.25, "tokenUsed": 10}))
    tb.set_state(cid, "i", "alpha", tb.REVIEW)
    out = team_apply_reviews(fake_task_input(
        correlationId=cid, reviewed=["alpha"],
        joined={"review_alpha__1": {"output": {"verdict": "lgtm", "findings": [], "costUsd": 1.5,
                                               "tokenUsed": 7, "model": "m"}}})).output_data
    assert out["reviewSpendRecorded"] == 1
    spend = budget_gate.read_spend(cid)
    assert spend["usd"] == 1.75
    assert budget_gate.by_role(spend["by_author"]) == {"planner": 0.25, "reviewer": 1.5}
    # A review-only wave is enough to move the gate to halt.
    assert budget_gate.gate(spend["usd"], 1.7)["state"] == "halt"


# ─────────────────────────── outcome: honest delivered/incomplete ──────────────
def test_outcome_delivered_when_all_done_and_green(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-out"
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS, contractPaths=[]))
    for i in ("alpha", "beta", "gamma"):
        tb.set_state(cid, "i", i, tb.DONE)
    out = team_outcome(fake_task_input(
        correlationId=cid, finalHead="abc123", testCycleState="tests_passed", testsPassed=True)).output_data
    assert out["delivered"] is True and out["verdict"] == "DELIVERED" and out["doneCount"] == 3
    assert "DELIVERED" in out["report"]


def test_outcome_incomplete_names_blocked_and_not_green(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-out2"
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS, contractPaths=[]))
    tb.set_state(cid, "i", "alpha", tb.DONE)
    tb.set_state(cid, "i", "beta", tb.BLOCKED, reason="rebase conflict")
    # gamma left open
    out = team_outcome(fake_task_input(
        correlationId=cid, finalHead="deadbeef", testCycleState="tests_failed_fix_unavailable",
        testsPassed=False)).output_data
    assert out["delivered"] is False and out["verdict"] == "INCOMPLETE"
    assert "beta" in out["report"] and "blocked" in out["report"] and "not green" in out["report"]
    assert out["blockedCount"] == 1 and out["openCount"] == 1


# ─────────────────────────── WP1.3 contract change = controlled event ──────────
def _contract_repo(tmp_path):
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "contract.py"), "w") as fh:
        fh.write("def api(x): ...\n")
    with open(os.path.join(shared, "a.py"), "w") as fh:
        fh.write("A = 1\n")
    _commit_all(shared, "base")
    return shared, git._current_branch(shared)


def _propose_contract_edit(monkeypatch, tmp_path, fake_task_input, cid):
    _isolate_board(monkeypatch, tmp_path)
    shared, branch = _contract_repo(tmp_path)
    items = [{"id": "a", "description": "a", "files": ["a.py"]},
             {"id": "b", "description": "b needs api(x, y)", "files": ["b.py", "contract.py"]}]
    team_plan_init(fake_task_input(correlationId=cid, subtasks=items, contractPaths=["contract.py"]))
    tb.set_state(cid, "i", "a", tb.DONE)
    tb.set_state(cid, "lead", "b", tb.IN_PROGRESS)
    _fork_worktree(shared, "b", branch, edit=("contract.py", "def api(x, y=None): ...\n"))
    out = team_integrate(fake_task_input(maxRescues=0, 
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, contractPaths=["contract.py"],
        groupIds="b", dispatched=[{"id": "b", "files": ["b.py", "contract.py"], "attempt": 0}],
        joined={})).output_data
    return shared, branch, out


def test_contract_edit_is_rejected_by_the_lead_not_merged(monkeypatch, tmp_path, fake_task_input):
    """Humans are passive: a frozen-contract edit is never held for an approval. The Lead rejects it the
    moment it is detected, the shared branch is untouched, and the next standup re-dispatches the
    proposer against the contract as-is."""
    cid = "run-contract"
    shared, branch, out = _propose_contract_edit(monkeypatch, tmp_path, fake_task_input, cid)
    assert out["contractEventCount"] == 1 and out["integratedCount"] == 0
    board = tb.fold_board(cid)
    assert board["items"]["b"]["reason"] == "contract_change_pending"
    assert not tb.open_exceptions(board["exceptions"])  # decided, nothing waits
    assert "api(x):" in open(os.path.join(shared, "contract.py")).read()  # shared branch untouched
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=["contract.py"],
                                      maxWidth=4)).output_data
    assert su["contractDecisions"][0]["decision"] == "reject"
    assert "b" in su["groupIds"].split(",")
    p = su["dynamicTasksInput"]["b"]["prompt"]
    assert "Do NOT edit the frozen contract" in p and "contract.py" in p
    assert "api(x):" in open(os.path.join(shared, "contract.py")).read()
    # Applied exactly once.
    su2 = team_standup(fake_task_input(repoPath=shared, correlationId=cid, maxWidth=4)).output_data
    assert su2["contractDecisions"] == []


def test_contract_churn_quarantines_past_cap(monkeypatch, tmp_path, fake_task_input):
    cid = "run-churn"
    shared, branch, _ = _propose_contract_edit(monkeypatch, tmp_path, fake_task_input, cid)
    tb.set_state(cid, "lead", "b", tb.IN_PROGRESS)
    # The rejected fork is gone with its wave; the re-dispatched item proposes again from a fresh fork.
    assert not os.path.isdir(os.path.join(shared, git.WORKTREES, "b"))
    _fork_worktree(shared, "b", branch, edit=("contract.py", "def api(x, y=None): ...\n"))
    out = team_integrate(fake_task_input(maxRescues=0, 
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, contractPaths=["contract.py"],
        groupIds="b", dispatched=[{"id": "b", "files": ["b.py", "contract.py"], "attempt": 0}],
        maxContractEvents=1, joined={})).output_data
    assert out["contractEventCount"] == 0 and out["blocked"][0]["reason"] == "contract_churn"
    kinds = {e["kind"] for e in tb.open_exceptions(tb.fold_board(cid)["exceptions"])}
    assert "contract_churn" in kinds


# ─────────────────────────── WP1.4 starvation exit (never a human gate) ────────
def test_starvation_names_why_and_never_waits_for_a_human(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    from team.tasks import team_monitor
    cid = "run-starve"
    items = [{"id": "a", "files": ["a.py"]}, {"id": "b", "files": ["b.py"], "dependsOn": ["a"]}]
    team_plan_init(fake_task_input(correlationId=cid, subtasks=items, contractPaths=[]))
    tb.set_state(cid, "i", "a", tb.BLOCKED, reason="empty delivery")
    # The monitor escalates 'a' (a notice) and the run is starved — it exits, it does not wait.
    m = team_monitor(fake_task_input(correlationId=cid, maxRescues=0, ceilingUsd=10)).output_data
    assert m["starved"] is True and m["starvedBy"] == "exceptions" and "gate" not in m
    tb.resolve_exception(cid, "lead", "a", "blocked", "noted")
    m = team_monitor(fake_task_input(correlationId=cid, maxRescues=0, ceilingUsd=10)).output_data
    assert m["starved"] is True and m["starvedBy"] == "dependency" and "b depends on a" in m["starvedDetail"]
    out = team_outcome(fake_task_input(correlationId=cid, testsPassed=False)).output_data
    assert "dependency-starved" in out["report"] and out["starvedBy"] == "dependency"


def test_swarm_workflows_never_wait_for_a_human():
    """Humans are passive observers of the swarm: no WAIT task, no humanAvailable input, anywhere."""
    import json
    wf = os.path.join(os.path.dirname(__file__), "..", "workflows")
    for name in ("team_build", "cc_program", "sdlc_intake"):
        text = open(os.path.join(wf, f"{name}.json")).read()
        assert '"WAIT"' not in text and "humanAvailable" not in text, name
    d = json.load(open(os.path.join(wf, "team_build.json")))
    loop = _task(d, "wave_loop")
    assert "$.starved === false" in loop["loopCondition"]


def test_reviewer_sees_human_directive_that_overrides_description(monkeypatch, tmp_path, fake_task_input):
    # Live-found gap (run b9446263): a reviewer, blind to the item's directives, asked to undo exactly
    # what a human had directed. The reviewer prompt must carry the item's steering, human first.
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-rev-dir"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[{"id": "a", "description": "no change",
                                                                 "files": ["a.py"]}], contractPaths=[]))
    tb.post_directive(cid, "human:ops@example.com", "a", "Create a.py containing exactly: A = 1", authority="human")
    tb.set_state(cid, "i", "a", tb.REVIEW)
    rb = team_review_build(fake_task_input(sharedRepo=shared, correlationId=cid, reviewing=[],
                                          finalHead=git.head(shared))).output_data
    p = rb["dynamicTasksInput"]["review_a"]["prompt"]
    assert "(human) Create a.py containing exactly: A = 1" in p and "OVERRIDES the description" in p


# ─────────────────────────── WP2 in-session feedback wiring ────────────────────
def test_standup_gives_each_implementer_its_feedback_channel(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-fb"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=[]))
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, maxWidth=2,
                                      checkpointEveryTurns=7)).output_data
    inp = su["dynamicTasksInput"]["gamma"]
    assert inp["feedbackCorrelationId"] == cid and inp["feedbackItem"] == "gamma"
    assert inp["feedbackSince"] > 0 and inp["feedbackEveryTurns"] == 1 and inp["checkpointEveryTurns"] == 7


def test_integrate_reopens_an_interrupted_item_without_merging(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-int-stop"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=[]))
    _fork_worktree(shared, "gamma", branch, edit=("gamma.py", "G = 'partial'\n"))
    base = git.head(shared)
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="gamma",
        dispatched=[{"id": "gamma", "files": ["gamma.py"], "attempt": 0}],
        joined={"gamma__2": {"output": {"interruptedBy": {"by": "human", "text": "stop"}}}})).output_data
    assert out["interrupted"][0]["item"] == "gamma" and out["integratedCount"] == 0
    assert git.head(shared) == base
    item = tb.fold_board(cid)["items"]["gamma"]
    assert item["state"] == tb.REOPENED and "interrupted by blocking feedback" in item["reason"]


# ─────────────────────────── WP3 best-of-N inside the swarm ────────────────────
def test_pick_best_prefers_green_then_smaller_change():
    cands = [
        {"ref": "a_c1", "delivered": True, "testsPassed": False, "deliveryOutcome": "passed", "changedFiles": 1},
        {"ref": "a_c2", "delivered": True, "testsPassed": True, "deliveryOutcome": "passed", "changedFiles": 3},
        {"ref": "a_c3", "delivered": True, "testsPassed": True, "deliveryOutcome": "passed", "changedFiles": 2},
        {"ref": "a_c4", "delivered": True, "touchesContract": True, "testsPassed": True, "changedFiles": 1},
        {"ref": "a_c5", "delivered": False, "testsPassed": True},
    ]
    assert tb.pick_best(cands)["ref"] == "a_c3"
    assert tb.pick_best([{"ref": "x", "delivered": False}]) is None


def test_best_of_dispatches_candidates_and_integrates_only_the_winner(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-bestof"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=[]))
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, maxWidth=4, bestOf=2)).output_data
    assert su["groupIds"] == "gamma_c1,gamma_c2"
    assert su["dispatched"][0]["candidates"] == ["gamma_c1", "gamma_c2"]
    from common import file_ownership
    assert file_ownership.owner_map(cid) == {"gamma.py": "gamma"}  # one claim per item, not per candidate
    _fork_worktree(shared, "gamma_c1", branch, edit=("gamma.py", "G = 1\n" * 20))
    _fork_worktree(shared, "gamma_c2", branch, edit=("gamma.py", "G = 1\n"))
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds=su["groupIds"],
        dispatched=su["dispatched"],
        joined={"gamma_c1__1": {"output": {"testsPassed": True, "deliveryOutcome": "passed", "costUsd": 0.2}},
                "gamma_c2__1": {"output": {"testsPassed": True, "deliveryOutcome": "passed", "costUsd": 0.3}}},
    )).output_data
    assert out["bestOf"][0]["winner"] == "gamma_c2"  # smaller diff wins among green candidates
    assert out["discardedCandidates"] == ["gamma_c1"] and out["reviewing"] == ["gamma"]
    assert open(os.path.join(shared, "gamma.py")).read() == "G = 1\n"
    assert git.git(shared, "rev-parse", "--verify", git.group_branch(shared, "gamma_c1"), check=False).code != 0
    from common import budget_gate
    assert budget_gate.read_spend(cid)["usd"] == 0.5  # every candidate's spend counts


# ─────────────────────────── WP6 acceptance tests: author ≠ check (H13) ────────
def test_acceptance_authoring_freezes_red_tests_and_discards_tautologies(monkeypatch, tmp_path, fake_task_input):
    from team.tasks import team_acceptance_build, team_acceptance_commit, _file_verdicts
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-accept"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS, contractPaths=["contract.py"]))
    tb.post_item(cid, "lead", {**_ITEMS[1], "acceptanceCriteria": ["beta registered"]})
    ab = team_acceptance_build(fake_task_input(correlationId=cid, repoPath=shared)).output_data
    # Only items with criteria get an author; the author may write only its own test file.
    assert ab["authored"] == {"alpha": "tests/acceptance/test_accept_alpha.py",
                              "beta": "tests/acceptance/test_accept_beta.py"}
    assert ab["dynamicTasksInput"]["accept_alpha"]["allowedWriteRoots"] == ["tests/acceptance/test_accept_alpha.py"]
    assert "must NOT implement" in ab["dynamicTasksInput"]["accept_alpha"]["prompt"]
    os.makedirs(os.path.join(shared, "tests", "acceptance"))
    for p in ab["authored"].values():
        with open(os.path.join(shared, p), "w") as fh:
            fh.write("def test_x():\n    assert False\n")
    _commit_all(shared, "acceptance")
    run = {"verificationState": "failed", "commands": [
        {"argv": ["pytest", "-q", "tests/acceptance/test_accept_alpha.py"], "exitCode": 1},
        {"argv": ["pytest", "-q", "tests/acceptance/test_accept_beta.py"], "exitCode": 0}]}
    out = team_acceptance_commit(fake_task_input(correlationId=cid, repoPath=shared, authored=ab["authored"],
                                                 run=run, joined={"accept_alpha": {"output": {"costUsd": 0.1}}})).output_data
    assert out["verdicts"] == {"alpha": "red", "beta": "green"}
    assert list(out["kept"]) == ["alpha"] and out["discarded"] == ["beta"]
    assert not os.path.exists(os.path.join(shared, "tests/acceptance/test_accept_beta.py"))
    assert "tests/acceptance/test_accept_alpha.py" in out["contractPaths"] and "contract.py" in out["contractPaths"]
    item = tb.fold_board(cid)["items"]["alpha"]
    assert item["acceptanceTests"] == ["tests/acceptance/test_accept_alpha.py"]
    assert item["testCommands"] == [{"argv": ["pytest", "-q", "tests/acceptance/test_accept_alpha.py"]}]
    # The implementer is told about its frozen check and its own test gate is pinned to it; an edit of
    # the acceptance test is now a frozen-contract (controlled) event.
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=["contract.py"],
                                      maxWidth=6)).output_data
    if "alpha" in su["dynamicTasksInput"]:
        assert "FROZEN ACCEPTANCE TESTS" in su["dynamicTasksInput"]["alpha"]["prompt"]
        assert su["dynamicTasksInput"]["alpha"]["testCommands"] == item["testCommands"]
        # Writable (so a wrong assertion can be amended) but never a required delivery (run a33d7061).
        assert su["dynamicTasksInput"]["alpha"]["acceptanceTests"] == item["acceptanceTests"]
        assert "tests/acceptance/test_accept_alpha.py" in su["dynamicTasksInput"]["alpha"]["allowedWriteRoots"]
    assert "tests/acceptance/test_accept_alpha.py" in su["contractPaths"]
    assert _file_verdicts({"a": "t.py"}, [{"argv": ["make", "test"], "exitCode": 1}], "failed") == {"a": "red"}


def test_outcome_refuses_delivery_when_acceptance_test_was_tampered(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-tamper"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    os.makedirs(os.path.join(shared, "tests"))
    with open(os.path.join(shared, "tests", "test_acc.py"), "w") as fh:
        fh.write("def test_a():\n    assert impl() == 1\n")
    _commit_all(shared, "acceptance")
    frozen = git.head(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=[]))
    from common import blackboard as bbx
    bbx.post_fact(cid, "lead", "contract", {"paths": ["tests/test_acc.py"], "frozen": True, "revision": 1,
                                            "acceptanceTests": ["tests/test_acc.py"], "acceptanceCommit": frozen})
    tb.set_state(cid, "i", "gamma", tb.DONE)
    with open(os.path.join(shared, "tests", "test_acc.py"), "w") as fh:
        fh.write("def test_a():\n    assert True\n")
    _commit_all(shared, "weaken")
    out = team_outcome(fake_task_input(correlationId=cid, repoPath=shared, finalHead=git.head(shared),
                                       testsPassed=True, testCycleState="tests_passed")).output_data
    assert out["delivered"] is False and out["acceptanceTampered"] == ["tests/test_acc.py"]
    assert "modified after they were frozen" in out["report"]


def test_acceptance_authors_work_in_isolated_worktrees_then_collect(monkeypatch, tmp_path, fake_task_input):
    # Live-found (run 25fe604e): concurrent authors in ONE worktree reverted each other's files at their
    # write-scope check. Each author now writes in its own worktree; collect gathers and commits them.
    from team.tasks import team_acceptance_build, team_acceptance_collect
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-collect"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {**_ITEMS[0], "acceptanceCriteria": ["a"]}, {**_ITEMS[2], "acceptanceCriteria": ["g"]}]))
    ab = team_acceptance_build(fake_task_input(correlationId=cid, repoPath=shared)).output_data
    wts = {ref: inp["worktreePath"] for ref, inp in ab["dynamicTasksInput"].items()}
    assert len(set(wts.values())) == 2 and all(shared + "/.cc-worktrees/" in w for w in wts.values())
    for gid, path in ab["authored"].items():
        ref = f"accept_{gid}"
        os.makedirs(os.path.dirname(os.path.join(wts[ref], path)), exist_ok=True)
        with open(os.path.join(wts[ref], path), "w") as fh:
            fh.write(f"def test_{gid}():\n    assert False\n")
    out = team_acceptance_collect(fake_task_input(repoPath=shared, authored=ab["authored"])).output_data
    assert sorted(out["collected"]) == sorted(ab["authored"].values()) and out["commit"] == git.head(shared)
    files = git.git(shared, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert sorted(files) == sorted(ab["authored"].values())
    assert not any(os.path.isdir(w) for w in wts.values())


def test_acceptance_author_is_told_which_siblings_are_built_concurrently(monkeypatch, tmp_path, fake_task_input):
    """st-lists' acceptance test called Set/Exists/Type from st-strings and st-keys, dispatched in the
    SAME wave: no item of that wave could pass its own gate alone, and the forks' test-cycle repair
    agents implemented each other's files to force it (run a33d7061). The author is told what it may
    rely on (the item's transitive dependencies, built first) and what does not exist yet."""
    from team.tasks import team_acceptance_build
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-accept-iso"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {"id": "core", "description": "core", "files": ["store/core.go"], "acceptanceCriteria": ["c"]},
        {"id": "keys", "description": "keys", "files": ["store/keys.go"], "dependsOn": ["core"],
         "acceptanceCriteria": ["k"]},
        {"id": "lists", "description": "lists", "files": ["store/lists.go"], "dependsOn": ["keys"],
         "acceptanceCriteria": ["l"]},
        {"id": "strings", "description": "strings", "files": ["store/strings.go"], "dependsOn": ["core"],
         "acceptanceCriteria": ["s"]}]))
    ab = team_acceptance_build(fake_task_input(correlationId=cid, repoPath=shared)).output_data
    lines = ab["dynamicTasksInput"]["accept_lists"]["prompt"].splitlines()
    rely = next(ln for ln in lines if ln.startswith("Dependencies built first"))
    assert "core (store/core.go)" in rely and "keys (store/keys.go)" in rely  # transitive, not only direct
    concurrent = next(ln for ln in lines if ln.startswith("Built concurrently or later"))
    assert "strings (store/strings.go)" in concurrent and "core (" not in concurrent and "keys (" not in concurrent
    core_prompt = ab["dynamicTasksInput"]["accept_core"]["prompt"]
    assert "satisfiable by this item's files" in core_prompt and "Dependencies built first" not in core_prompt
    assert all(f"{o} (store/{o}.go)" in core_prompt for o in ("keys", "lists", "strings"))
    # sv-core's test asserted PING is unknown because no sibling had registered it yet; sv-connection then
    # registered PING and the frozen test was red at the integrated head (run 299b4907).
    assert "never assert that a sibling's behaviour is ABSENT" in core_prompt
    assert "re-run on the shared branch after every item lands" in core_prompt


# ─────────────────── write scope: the Lead widens it from a recorded refusal (run fc6467b7) ───────────
def _scope_run(monkeypatch, tmp_path, fake_task_input, cid):
    """core owns store.go, lru owns lru.go, contract.go is the frozen contract."""
    _isolate_board(monkeypatch, tmp_path)
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    for name in ("store.go", "lru.go", "contract.go"):
        with open(os.path.join(shared, name), "w") as fh:
            fh.write("package store\n")
    _commit_all(shared, "base")
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=["contract.go"], subtasks=[
        {"id": "core", "description": "store core", "files": ["store.go"]},
        {"id": "lru", "description": "lru eviction", "files": ["lru.go"]}]))
    return shared


def test_standup_widens_write_scope_once_the_owner_has_landed(monkeypatch, tmp_path, fake_task_input):
    """st-lru's criteria needed eviction hooks in store.go, owned by st-core; the guard refused every
    attempt and the item burned its review rounds on work-arounds until the program halted (fc6467b7).
    The refusal is the Lead's evidence: once the owner is done the file joins the item's scope."""
    from common import file_ownership
    cid = "run-scope"
    shared = _scope_run(monkeypatch, tmp_path, fake_task_input, cid)
    tb.set_state(cid, "lead", "core", tb.DONE)
    tb.set_state(cid, "lead", "lru", tb.REOPENED, reason="peer review requested changes")
    file_ownership.bb.post_fact(cid, "integrator", "session",
                                {"item": "lru", "candidate": "lru", "deniedWrites": ["store.go", "contract.go"]})

    out = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=["contract.go"],
                                       maxWidth=6)).output_data
    assert out["groupIds"] == "lru" and out["scopeWaits"] == []
    roots = out["dynamicTasksInput"]["lru"]["allowedWriteRoots"]
    assert "store.go" in roots and "contract.go" not in roots  # a frozen-contract path is never widened
    assert "edit ONLY these): lru.go, store.go" in out["dynamicTasksInput"]["lru"]["prompt"]
    board = tb.fold_board(cid)
    assert board["items"]["lru"]["files"] == ["lru.go", "store.go"]  # the reviewer's diff covers it too
    notices = [e for e in board["exceptions"] if e["kind"] == "write_scope"]
    assert notices and notices[-1]["resolved"] is True and "store.go" in notices[-1]["detail"]
    assert tb.open_exceptions(board["exceptions"]) == []  # a notice for awareness, nothing waits on it


def test_standup_holds_the_item_while_an_open_item_still_owns_the_file(monkeypatch, tmp_path, fake_task_input):
    from common import file_ownership
    cid = "run-scope-wait"
    shared = _scope_run(monkeypatch, tmp_path, fake_task_input, cid)
    tb.set_state(cid, "lead", "core", tb.IN_PROGRESS)
    tb.set_state(cid, "lead", "lru", tb.REOPENED)
    file_ownership.bb.post_fact(cid, "integrator", "session", {"item": "lru", "candidate": "lru",
                                                               "deniedWrites": ["store.go"]})

    out = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=["contract.go"],
                                       maxWidth=6)).output_data
    assert out["dispatchedCount"] == 0 and out["scopeWaits"] == ["lru waits for core to land store.go"]
    assert tb.fold_board(cid)["items"]["lru"]["files"] == ["lru.go"]

    # A blocked owner's claim is over (its fork is never integrated): the item proceeds.
    tb.set_state(cid, "lead", "core", tb.BLOCKED, reason="review_rounds_exhausted")
    out2 = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=["contract.go"],
                                        maxWidth=6)).output_data
    assert out2["groupIds"] == "lru" and "store.go" in out2["dynamicTasksInput"]["lru"]["allowedWriteRoots"]


def test_standup_widens_one_item_at_a_time_when_several_were_refused_the_same_file(monkeypatch, tmp_path,
                                                                                   fake_task_input):
    """Four store items refused store/store.go were all widened to it in ONE standup and edited it
    concurrently (run a062184f: six duplication alerts, three extra review rounds). The first widened
    item owns the file for the rest of the standup; the others wait for it to land."""
    from common import file_ownership
    cid = "run-scope-serial"
    shared = _scope_run(monkeypatch, tmp_path, fake_task_input, cid)
    with open(os.path.join(shared, "expiry.go"), "w") as fh:
        fh.write("package store\n")
    _commit_all(shared, "expiry")
    tb.post_item(cid, "lead", {"id": "expiry", "description": "ttl expiry", "files": ["expiry.go"]})
    tb.set_state(cid, "lead", "core", tb.DONE)
    for sid in ("lru", "expiry"):
        tb.set_state(cid, "lead", sid, tb.REOPENED, reason="peer review requested changes")
        file_ownership.bb.post_fact(cid, "integrator", "session",
                                    {"item": sid, "candidate": sid, "deniedWrites": ["store.go"]})

    out = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=["contract.go"],
                                       maxWidth=6)).output_data
    assert out["groupIds"] == "expiry" and out["scopeWaits"] == ["lru waits for expiry to land store.go"]
    board = tb.fold_board(cid)
    assert board["items"]["expiry"]["files"] == ["expiry.go", "store.go"] and board["items"]["lru"]["files"] == ["lru.go"]

    # Once the owner lands (the integrator releases its claims), the next item is widened and dispatched.
    tb.set_state(cid, "lead", "expiry", tb.DONE)
    for path in ("expiry.go", "store.go"):
        file_ownership.release(cid, "expiry", path)
    out2 = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=["contract.go"],
                                        maxWidth=6)).output_data
    assert out2["groupIds"] == "lru" and "store.go" in out2["dynamicTasksInput"]["lru"]["allowedWriteRoots"]


def test_standup_never_widens_an_item_outside_the_directories_its_team_spans(monkeypatch, tmp_path,
                                                                             fake_task_input):
    """The design team's d-store (a documents item) was refused store/errors.go and store/store.go and
    was widened to them: a documents team took ownership of code the breakdown then assigned to the
    store team (run a33d7061). A team's write authority ends at the directories its items span; the
    refusal is a resolved write_scope notice, and an in-span path is still widened."""
    from common import file_ownership
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-scope-span"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    os.makedirs(os.path.join(shared, "docs", "design"))
    with open(os.path.join(shared, "docs", "design", "store.md"), "w") as fh:
        fh.write("# store\n")
    _commit_all(shared, "base")
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {"id": "d-store", "description": "store design", "files": ["docs/design/store.md"], "deliverable": "docs"},
        {"id": "d-arch", "description": "architecture", "files": ["docs/design/architecture.md"],
         "deliverable": "docs"}]))
    tb.set_state(cid, "lead", "d-arch", tb.DONE)
    tb.set_state(cid, "lead", "d-store", tb.REOPENED, reason="peer review requested changes")
    file_ownership.bb.post_fact(cid, "integrator", "session", {
        "item": "d-store", "candidate": "d-store",
        "deniedWrites": ["store/errors.go", "docs/design/glossary.md"]})

    out = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=[], maxWidth=6)).output_data
    roots = out["dynamicTasksInput"]["d-store"]["allowedWriteRoots"]
    assert "docs/design/glossary.md" in roots and "store/errors.go" not in roots
    assert out["scopeRefusals"] == ["refused to widen d-store's write scope to store/errors.go: outside the "
                                    "directories this team's items span (docs/design)"]
    board = tb.fold_board(cid)
    refusals = [e for e in board["exceptions"] if e["kind"] == "write_scope" and "refused" in e["detail"]]
    assert refusals and refusals[-1]["resolved"] is True
    assert tb.open_exceptions(board["exceptions"]) == []  # a notice for awareness, nothing waits on it
    # The same refusal is not re-posted at every standup.
    team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=[], maxWidth=6))
    again = [e for e in tb.fold_board(cid)["exceptions"] if e["kind"] == "write_scope" and "refused" in e["detail"]]
    assert len(again) == len(refusals)


# ─────────────────────────── run ec0fb6dc: the head after an approved amendment ─────────────────
def test_apply_reviews_reports_the_head_after_an_approved_amendment(monkeypatch, tmp_path, fake_task_input):
    """The server team's reviewer approved amending a frozen acceptance test; the merge moved the shared
    head, but the wave's finalHead was the integrate step's (pre-merge) head, so the outcome judged the
    re-frozen test "modified after frozen" and the final test cycle verified the wrong commit — the
    program blocked cmd and integration on a delivered subsystem (run ec0fb6dc). The wave's finalHead is
    now read after apply_reviews, and a decided fork's worktree is removed."""
    _isolate_board(monkeypatch, tmp_path)
    from common import blackboard as bbx
    cid = "run-amend-head"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    os.makedirs(os.path.join(shared, "tests"))
    with open(os.path.join(shared, "tests", "test_acc.py"), "w") as fh:
        fh.write("def test_g():\n    assert gamma() == 1\n")
    _commit_all(shared, "acceptance")
    frozen = git.head(shared)
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=["tests/test_acc.py"]))
    bbx.post_fact(cid, "lead", "contract", {"paths": ["tests/test_acc.py"], "frozen": True, "revision": 1,
                                            "acceptanceTests": ["tests/test_acc.py"], "acceptanceCommit": frozen})
    wt = _fork_worktree(shared, "gamma", branch,
                        edit=("tests/test_acc.py", "def test_g():\n    assert gamma() == 2\n"))
    # Integrate holds the amendment unmerged for the reviewer: the fork's worktree must survive.
    integ = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="gamma",
        contractPaths=["tests/test_acc.py"], enablePeerReview=True,
        dispatched=[{"id": "gamma", "files": ["gamma.py"], "attempt": 0}],
        joined={"gamma__1": {"output": {"testsPassed": True, "deliveryOutcome": "passed"}}})).output_data
    assert integ["reviewing"] == ["gamma"] and integ["removedForks"] == [] and os.path.isdir(wt)
    assert git.head(shared) == frozen
    out = team_apply_reviews(fake_task_input(
        correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=["gamma"], maxReviewRounds=2,
        joined={"review_gamma": {"output": {"verdict": "lgtm", "findings": []}}})).output_data
    assert out["doneCount"] == 1
    assert out["finalHead"] == git.head(shared) and out["finalHead"] != frozen
    assert not os.path.isdir(wt)  # decided: merged onto the shared branch, the fork is finished
    ran = [{"argv": ["make", "test"], "exitCode": 0}, {"argv": ["pytest", "-q", "tests/test_acc.py"], "exitCode": 0}]
    good = team_outcome(fake_task_input(correlationId=cid, repoPath=shared, finalHead=out["finalHead"],
                                        testsPassed=True, testCycleState="tests_passed",
                                        testCommandsRan=ran)).output_data
    assert good["delivered"] is True and good["acceptanceTampered"] == []
    # Handed the integrate step's stale head, the outcome still judges the branch as it is: a frozen
    # test committed after the recorded head is not "modified after frozen" (runs ec0fb6dc, 884f7eec).
    stale = team_outcome(fake_task_input(correlationId=cid, repoPath=shared, finalHead=frozen,
                                         testsPassed=True, testCycleState="tests_passed",
                                         testCommandsRan=ran)).output_data
    assert stale["delivered"] is True and stale["acceptanceTampered"] == [] and stale["finalHead"] == git.head(shared)
    import json
    d = json.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "team_build.json")))
    loop = _task(d, "wave_loop")
    capture = [t for t in loop["loopOver"] if t["taskReferenceName"] == "capture_round"][0]
    assert capture["inputParameters"]["finalHead"] == "${replan.output.finalHead}"
    by_ref = {ref: _task(d, ref) for ref in ("loop_init", "outcome", "tests", "tests_fork", "wave_loop", "acceptance_build", "acceptance_gate")}
    assert "set_candidate" not in by_ref  # the final cycle verifies its own fork's head (tests_fork)
    assert by_ref["outcome"]["inputParameters"]["finalHead"] == "${workflow.variables.finalHead}"


def test_a_rejected_amendment_tells_the_implementer_the_frozen_test_stands(monkeypatch, tmp_path, fake_task_input):
    """resp-reader's reviewer kept answering a frozen-test amendment with "update the test". Each answer
    discarded the amendment and told the implementer to edit a file its brief calls frozen, so it pushed
    back until both rescues were spent (run 0588dafa). The reviewer now only approves or rejects, and a
    rejection tells the implementer the test stands."""
    _isolate_board(monkeypatch, tmp_path)
    from common import blackboard as bbx
    cid = "run-amend-reject"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    os.makedirs(os.path.join(shared, "tests"))
    original = "def test_g():\n    assert gamma() == 1\n"
    with open(os.path.join(shared, "tests", "test_acc.py"), "w") as fh:
        fh.write(original)
    _commit_all(shared, "acceptance")
    frozen = git.head(shared)
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=["tests/test_acc.py"]))
    bbx.post_fact(cid, "lead", "contract", {"paths": ["tests/test_acc.py"], "frozen": True, "revision": 1,
                                            "acceptanceTests": ["tests/test_acc.py"], "acceptanceCommit": frozen})
    _fork_worktree(shared, "gamma", branch, edit=("tests/test_acc.py", "def test_g():\n    assert gamma() == 2\n"))
    team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="gamma",
        contractPaths=["tests/test_acc.py"], enablePeerReview=True,
        dispatched=[{"id": "gamma", "files": ["gamma.py"], "attempt": 0}],
        joined={"gamma__1": {"output": {"testsPassed": True, "deliveryOutcome": "passed"}}}))
    built = team_review_build(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch,
                                              reviewing=["gamma"], finalHead=frozen, waveBase=frozen)).output_data
    prompt = built["dynamicTasksInput"]["review_gamma"]["prompt"]
    assert "approve or reject, you cannot send the test back" in prompt
    assert "A weakness the original test also had is not grounds to reject" in prompt
    assert "never to the test" in prompt

    out = team_apply_reviews(fake_task_input(
        correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=["gamma"], maxReviewRounds=2,
        joined={"review_gamma": {"output": {"verdict": "changes", "findings": ["gamma() must return 1"]}}})).output_data
    assert out["doneCount"] == 0 and git.head(shared) == frozen
    assert open(os.path.join(shared, "tests", "test_acc.py")).read() == original
    text = [f["data"]["text"] for f in bbx.read_facts(cid, kind="directive") if f["data"].get("target") == "gamma"][-1]
    assert "was rejected and discarded" in text and "do not edit them" in text and "gamma() must return 1" in text
    assert "Address the peer-review findings" not in text


def test_integrate_removes_the_worktrees_of_decided_forks(monkeypatch, tmp_path, fake_task_input):
    """Fork worktrees stayed registered after their wave (18 of them for one finished run). Claude Code's
    Bash sandbox adds deny paths for every registered worktree; at 171 the profile exceeded the OS exec
    argument limit and every implementer's every Bash call failed at spawn — 591 E2BIG failures, no
    agent could build or test (run ec0fb6dc). A decided fork — integrated, re-queued or discarded —
    loses its worktree and branch at the end of the wave; the work is on the shared branch."""
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-int-wt"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2], _ITEMS[0]], contractPaths=[]))
    wt_g = _fork_worktree(shared, "gamma", branch, edit=("gamma.py", "G = 1\n"))
    wt_a = _fork_worktree(shared, "alpha", branch)  # produced nothing: re-queued
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="gamma,alpha",
        dispatched=[{"id": "gamma", "files": ["gamma.py"], "attempt": 0},
                    {"id": "alpha", "files": ["alpha.py", "registry.py"], "attempt": 0}],
        joined={"gamma__1": {"output": {"testsPassed": True, "deliveryOutcome": "passed"}},
                "alpha__1": {"output": {"deliveryOutcome": "incomplete_delivery"}}})).output_data
    assert out["integratedCount"] == 1 and out["retryCount"] == 1
    assert sorted(out["removedForks"]) == ["alpha", "gamma"]
    assert not os.path.isdir(wt_g) and not os.path.isdir(wt_a)
    for gid in ("gamma", "alpha"):
        assert git.git(shared, "rev-parse", "--verify", git.group_branch(shared, gid), check=False).code != 0
    assert open(os.path.join(shared, "gamma.py")).read() == "G = 1\n"
    assert git.WORKTREES not in git.git(shared, "worktree", "list", "--porcelain").stdout


# ───────── the integration check: frozen tests re-run at the integrated head (run 299b4907) ─────────
def _check_repo(tmp_path, cid, fake_task_input):
    """alpha landed with a frozen test that asserts beta's key is ABSENT (true on alpha's fork); beta's
    frozen test is red until beta lands. Returns (shared, branch)."""
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    os.makedirs(os.path.join(shared, "tests", "acceptance"))
    with open(os.path.join(shared, "registry.py"), "w") as fh:
        fh.write("H = {'alpha': 1}\n")
    head = ("import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname("
            "os.path.abspath(__file__)))))\nfrom registry import H\n\n")
    with open(os.path.join(shared, "tests", "acceptance", "test_accept_alpha.py"), "w") as fh:
        fh.write(head + "def test_alpha():\n    assert H['alpha'] == 1 and 'beta' not in H\n")
    with open(os.path.join(shared, "tests", "acceptance", "test_accept_beta.py"), "w") as fh:
        fh.write(head + "def test_beta():\n    assert H['beta'] == 2\n")
    _commit_all(shared, "alpha landed; frozen tests")
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS[:2], contractPaths=[]))
    for gid in ("alpha", "beta"):
        path = f"tests/acceptance/test_accept_{gid}.py"
        tb.post_item(cid, "lead", {**next(x for x in _ITEMS if x["id"] == gid), "acceptanceTests": [path],
                                   "testCommands": [{"argv": ["pytest", "-q", path]}]})
    tb.set_state(cid, "lead", "alpha", tb.DONE)
    tb.set_state(cid, "lead", "beta", tb.IN_PROGRESS)
    return shared, git._current_branch(shared)


def test_integrate_reopens_an_item_whose_frozen_test_fails_at_the_integrated_head(monkeypatch, tmp_path,
                                                                                    fake_task_input):
    """H13 held on each fork only. sv-core's frozen test asserted PING is an unknown command — true on its
    fork; sv-connection then registered PING. The final cycle ran only `make test`, which never executes
    the acceptance suite, so the program delivered 6/6 with 2 of its 290 frozen tests red at the head
    (run 299b4907). The integrator now runs every integrated item's frozen tests at the integrated head:
    a red one re-opens its owner with the failure as its directive, and the just-landed sibling still
    goes to review."""
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-check"
    shared, branch = _check_repo(tmp_path, cid, fake_task_input)
    _fork_worktree(shared, "beta", branch, edit=("registry.py", "H = {'alpha': 1, 'beta': 2}\n"))
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, contractPaths=[], groupIds="beta",
        dispatched=[{"id": "beta", "files": ["registry.py"], "attempt": 0}], enablePeerReview=True,
        sandboxChecks=False, joined={"beta__1": {"output": {"testsPassed": True, "deliveryOutcome": "passed"}}}
    )).output_data
    assert out["integratedCount"] == 1 and out["reviewing"] == ["beta"]
    assert out["check"]["state"] == "failed" and out["check"]["checked"] == ["alpha", "beta"]
    assert list(out["check"]["red"]) == ["alpha"] and "test_alpha" in out["check"]["red"]["alpha"]
    assert out["retryCount"] == 1 and out["retry"][0]["id"] == "alpha"
    board = tb.fold_board(cid)
    assert board["items"]["beta"]["state"] == tb.REVIEW
    assert board["items"]["alpha"]["state"] == tb.REOPENED and board["items"]["alpha"]["attempt"] == 1
    assert "fail at the integrated head" in board["items"]["alpha"]["reason"]
    directive = [d for d in board["directives"] if d.get("target") == "alpha"][-1]
    assert "FAIL on the shared branch" in directive["text"] and "test_alpha" in directive["text"]
    assert "peer reviewer decides the amendment" in directive["text"]
    # Green at the head: alpha's re-dispatch amended the assertion (reviewed and merged), nothing re-opens.
    with open(os.path.join(shared, "tests", "acceptance", "test_accept_alpha.py"), "w") as fh:
        fh.write("import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname("
                 "os.path.abspath(__file__)))))\nfrom registry import H\n\ndef test_alpha():\n"
                 "    assert H['alpha'] == 1 and 'no-item-claims-this' not in H\n")
    _commit_all(shared, "alpha: amended assertion")
    tb.set_state(cid, "lead", "alpha", tb.DONE)
    tb.set_state(cid, "lead", "beta", tb.DONE)
    again = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, contractPaths=[], groupIds="",
        dispatched=[], enablePeerReview=True, sandboxChecks=False, joined={})).output_data
    assert again["check"] == {"state": "passed", "checked": ["alpha", "beta"], "red": {}}
    assert again["retryCount"] == 0 and tb.fold_board(cid)["items"]["alpha"]["state"] == tb.DONE


def test_integrate_check_skips_a_fork_held_for_an_amendment_and_items_without_a_frozen_test(
        monkeypatch, tmp_path, fake_task_input):
    """A fork held unmerged for the reviewer's amendment decision is red at the head by construction and
    must not be re-opened for it; an item with no frozen test is not checked."""
    _isolate_board(monkeypatch, tmp_path)
    from common import blackboard as bbx
    cid = "run-check-held"
    shared, branch = _check_repo(tmp_path, cid, fake_task_input)
    tb.set_state(cid, "lead", "beta", tb.REVIEW)  # integrated earlier, still awaiting its reviewer
    bbx.post_fact(cid, "integrator", "acceptance_amendment", {
        "item": "beta", "candidate": "beta", "paths": ["tests/acceptance/test_accept_beta.py"]})
    tb.post_item(cid, "lead", {**_ITEMS[2]})  # gamma: no acceptance test
    tb.set_state(cid, "lead", "gamma", tb.DONE)
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, contractPaths=[], groupIds="",
        dispatched=[], enablePeerReview=True, sandboxChecks=False, joined={})).output_data
    assert out["check"] == {"state": "passed", "checked": ["alpha"], "red": {}}
    assert tb.fold_board(cid)["items"]["beta"]["state"] == tb.REVIEW


def test_outcome_refuses_delivery_when_the_final_cycle_ran_no_frozen_test(monkeypatch, tmp_path,
                                                                           fake_task_input):
    """`make test` green is not evidence about the frozen acceptance tests: the final cycle's executed
    commands must name each of them, or the delivered head is unproven against its check."""
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-unproven"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    os.makedirs(os.path.join(shared, "tests"))
    with open(os.path.join(shared, "tests", "test_acc.py"), "w") as fh:
        fh.write("def test_a():\n    assert impl() == 1\n")
    _commit_all(shared, "acceptance")
    frozen = git.head(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=[]))
    from common import blackboard as bbx
    bbx.post_fact(cid, "lead", "contract", {"paths": ["tests/test_acc.py"], "frozen": True, "revision": 1,
                                            "acceptanceTests": ["tests/test_acc.py"], "acceptanceCommit": frozen})
    tb.post_item(cid, "lead", {**_ITEMS[2], "acceptanceTests": ["tests/test_acc.py"],
                               "testCommands": [{"argv": ["pytest", "-q", "tests/test_acc.py"]}]})
    tb.set_state(cid, "i", "gamma", tb.DONE)
    only_make = team_outcome(fake_task_input(
        correlationId=cid, repoPath=shared, finalHead=frozen, testsPassed=True, testCycleState="tests_passed",
        testCommandsRan=[{"argv": ["make", "test"], "exitCode": 0}])).output_data
    assert only_make["delivered"] is False and only_make["acceptanceUnproven"] == ["tests/test_acc.py"]
    assert "ran no command for frozen acceptance test(s) tests/test_acc.py" in only_make["report"]
    proven = team_outcome(fake_task_input(
        correlationId=cid, repoPath=shared, finalHead=frozen, testsPassed=True, testCycleState="tests_passed",
        testCommandsRan=[{"argv": ["make", "test"], "exitCode": 0},
                         {"argv": ["pytest", "-q", "tests/test_acc.py"], "exitCode": 0}])).output_data
    assert proven["delivered"] is True and proven["acceptanceUnproven"] == []
    # Wiring: the final test cycle runs on its own fork (tests_fork), verifies that fork's head, carries
    # the DONE items' frozen-test commands (apply_reviews → capture_round) as its prior obligation and
    # repairs only inside the team's span; the outcome reads the commands it ran and integrates the
    # fork; the integrator gets the sandbox flag for the per-wave check.
    import json
    d = json.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "team_build.json")))
    by_ref = {ref: _task(d, ref) for ref in ("loop_init", "outcome", "tests", "tests_fork", "wave_loop", "acceptance_build", "acceptance_gate")}
    assert by_ref["tests_fork"]["name"] == "worktree_add" and by_ref["tests_fork"]["inputParameters"] == {
        "repoPath": "${workspace.output.worktreePath}", "name": "tests"}
    tests = by_ref["tests"]["inputParameters"]
    assert tests["repoPath"] == "${tests_fork.output.worktreePath}"
    assert tests["candidateCommit"] == "${tests_fork.output.initialCommit}"
    assert tests["priorVerification"] == {"commands": "${workflow.variables.acceptanceCommands}"}
    assert tests["allowedWriteRoots"] == "${workflow.variables.testWriteRoots}"
    outcome = by_ref["outcome"]["inputParameters"]
    assert outcome["testCommandsRan"] == "${tests.output.commands}"
    assert outcome["testsWorktree"] == "${tests_fork.output.worktreePath}"
    assert outcome["testsBranch"] == "${tests_fork.output.branch}"
    assert outcome["sharedBranch"] == "${workspace.output.branch}"
    assert by_ref["loop_init"]["inputParameters"]["acceptanceCommands"] == []
    assert by_ref["loop_init"]["inputParameters"]["testWriteRoots"] == []
    loop = _task(d, "wave_loop")
    in_loop = {t["taskReferenceName"]: t for t in loop["loopOver"]}
    assert in_loop["integrate"]["inputParameters"]["sandboxChecks"] == "${workflow.input.sandboxChecks}"
    assert in_loop["capture_round"]["inputParameters"]["acceptanceCommands"] == "${replan.output.acceptanceCommands}"
    assert in_loop["capture_round"]["inputParameters"]["testWriteRoots"] == "${replan.output.writeRoots}"
    assert in_loop["monitor"]["inputParameters"]["dispatchedCount"] == "${standup.output.dispatchedCount}"
    assert in_loop["monitor"]["inputParameters"]["scopeWaits"] == "${standup.output.scopeWaits}"


# ──────── the final test cycle on its own fork; done-items check; scope-wait deadlocks (run a4f1ff1f) ────────
def test_outcome_integrates_a_green_final_cycle_repair_from_its_own_fork(monkeypatch, tmp_path, fake_task_input):
    """The final test cycle's repair agent ran in the SHARED worktree, where the agent runner resets the
    tree to the head the session started from: the store team's repair commit was parented on a commit
    before the resp team's r-reader merge (the program branch lost that merge) and left an untracked
    internal/resp/writer.go that aborted r-writer's merge — the resp team failed on a git error (run
    a4f1ff1f). The cycle now runs on its own fork; the outcome integrates a green repair like any fork,
    discards a red one, and removes the fork either way."""
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-tests-fork"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=[]))
    tb.set_state(cid, "lead", "gamma", tb.DONE)
    fork = git.worktree_add(shared, "tests")
    with open(os.path.join(fork["worktreePath"], "seed.py"), "w") as fh:
        fh.write("x = 2\n")
    _commit_all(fork["worktreePath"], "test_cycle repair")
    repaired = git.head(fork["worktreePath"])
    out = team_outcome(fake_task_input(
        correlationId=cid, repoPath=shared, sharedRepo=shared, sharedBranch=branch,
        testsWorktree=fork["worktreePath"], testsBranch=fork["branch"], finalHead=fork["initialCommit"],
        testsPassed=True, testCycleState="tests_passed",
        testCommandsRan=[{"argv": ["make", "test"], "exitCode": 0}])).output_data
    assert out["delivered"] is True and out["repairIntegrated"] is True and out["repairDropped"] == ""
    assert out["finalHead"] == git.head(shared) == repaired  # fast-forwarded, never rewritten
    assert git.git(shared, "rev-parse", f"{repaired}^").stdout.strip() == fork["initialCommit"]
    assert not os.path.isdir(fork["worktreePath"])
    # A red repair is discarded: the branch stays where it was, and the team is not DELIVERED.
    fork2 = git.worktree_add(shared, "tests")
    with open(os.path.join(fork2["worktreePath"], "seed.py"), "w") as fh:
        fh.write("x = 3\n")
    _commit_all(fork2["worktreePath"], "red repair")
    red = team_outcome(fake_task_input(
        correlationId=cid, repoPath=shared, sharedRepo=shared, sharedBranch=branch,
        testsWorktree=fork2["worktreePath"], testsBranch=fork2["branch"], finalHead=git.head(shared),
        testsPassed=False, testCycleState="tests_failed_fix_unavailable", testCommandsRan=[])).output_data
    assert red["delivered"] is False and red["repairIntegrated"] is False
    assert "repair commit was not integrated" in red["report"] and git.head(shared) == repaired
    assert not os.path.isdir(fork2["worktreePath"])


def test_apply_reviews_reports_the_done_items_check_and_the_team_span(monkeypatch, tmp_path, fake_task_input):
    """The final cycle's obligations come from the board at the end of the loop: the frozen tests of the
    items that are DONE (an undone item's test is red by construction — the store team's final cycle ran
    st-core's test and its repair agent set out to implement st-core with write roots derived from the
    resp team's last commit, run a4f1ff1f) and the files this team's items span."""
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-replan-check"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS, contractPaths=[]))
    for gid in ("alpha", "beta"):
        path = f"tests/acceptance/test_accept_{gid}.py"
        tb.post_item(cid, "lead", {**next(x for x in _ITEMS if x["id"] == gid), "acceptanceTests": [path],
                                   "testCommands": [{"argv": ["pytest", "-q", path]}]})
    tb.set_state(cid, "lead", "alpha", tb.DONE)  # beta stays in the backlog: its red test is no obligation
    out = team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared,
                                             sharedBranch=git._current_branch(shared), reviewed=[],
                                             joined={})).output_data
    assert out["acceptanceCommands"] == [{"argv": ["pytest", "-q", "tests/acceptance/test_accept_alpha.py"]}]
    assert out["writeRoots"] == ["alpha.py", "beta.py", "gamma.py", "registry.py"]


def test_lead_widens_scope_when_the_file_owner_depends_on_the_waiting_item():
    """st-stats owned stats.go and depended on st-core, which needed a Stats stub there: st-core waited
    for st-stats, st-stats waited for st-core, and the team spun 14 empty waves to maxWaves (run
    a4f1ff1f). A holder that cannot start until the waiting item lands is a cycle: widen now."""
    from team.tasks import _widen_scope
    items = {"core": {"id": "core", "files": ["store/store.go"], "state": tb.REOPENED},
             "stats": {"id": "stats", "files": ["store/stats.go"], "state": tb.BACKLOG, "dependsOn": ["core"]},
             "keys": {"id": "keys", "files": ["store/keys.go"], "state": tb.BACKLOG}}
    widen, held, refused = _widen_scope(items, "core", ["store/store.go"], ["store/stats.go"], [])
    assert widen == ["store/stats.go"] and held == [] and refused == []
    # An owner that does not depend on the item can land first: the item waits for it, as before.
    widen, held, refused = _widen_scope(items, "core", ["store/store.go"], ["store/keys.go"], [])
    assert widen == [] and held == ["core waits for keys to land store/keys.go"]


def test_the_lead_breaks_a_deadlock_instead_of_ending_the_run(monkeypatch, tmp_path, fake_task_input):
    """A board where no work can move is the Lead's to resolve. Each stuck root gets a helper, and an item
    waiting on a dependency that can never land stops waiting for it."""
    from team.tasks import team_monitor
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-deadlock-hold"
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS[:2], contractPaths=[]))
    tb.set_state(cid, "lead", "alpha", tb.REOPENED)
    held = ["alpha waits for beta to land registry.py"]
    m = team_monitor(fake_task_input(correlationId=cid, ceilingUsd=10, dispatchedCount=0, scopeWaits=held,
                                     maxRescues=2)).output_data
    assert m["starved"] is False
    board = tb.fold_board(cid)
    assert board["items"]["alpha"]["state"] == tb.REOPENED and "deadlock" in board["items"]["alpha"]["reason"]

    # A dependency on no item at all: only that edge goes, the real order stands.
    cid = "run-deadlock-ghost"
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {"id": "alpha", "description": "a", "files": ["a.py"], "dependsOn": ["beta"]},
        {"id": "beta", "description": "b", "files": ["b.py"], "dependsOn": ["ghost"]}]))
    m = team_monitor(fake_task_input(correlationId=cid, ceilingUsd=10, dispatchedCount=0, scopeWaits=[],
                                     maxRescues=2)).output_data
    items = tb.fold_board(cid)["items"]
    assert m["starved"] is False and [i["id"] for i in tb.ready_items(items)] == ["beta"]
    assert items["alpha"]["dependsOn"] == ["beta"] and items["beta"]["dependsOn"] == []

    # A dependency cycle: nothing is ready and nothing is blocked.
    cid = "run-deadlock-cycle"
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {"id": "a", "description": "a", "files": ["a.py"], "dependsOn": ["b"]},
        {"id": "b", "description": "b", "files": ["b.py"], "dependsOn": ["a"]}]))
    m = team_monitor(fake_task_input(correlationId=cid, ceilingUsd=10, dispatchedCount=0, scopeWaits=[],
                                     maxRescues=2)).output_data
    assert m["starved"] is False
    assert [i["id"] for i in tb.ready_items(tb.fold_board(cid)["items"])] == ["a"]

    # A blocked root is reopened with help, freeing what waits on it.
    cid = "run-deadlock-blocked"
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {"id": "a", "description": "a", "files": ["a.py"]},
        {"id": "b", "description": "b", "files": ["b.py"], "dependsOn": ["a"]}]))
    tb.set_state(cid, "integrator", "a", tb.BLOCKED, reason="stuck")
    m = team_monitor(fake_task_input(correlationId=cid, ceilingUsd=10, dispatchedCount=0, scopeWaits=[],
                                     maxRescues=2)).output_data
    assert m["starved"] is False and tb.fold_board(cid)["items"]["a"]["state"] == tb.REOPENED


def test_monitor_ends_the_run_when_ready_work_is_held_and_nothing_is_in_flight(monkeypatch, tmp_path,
                                                                                  fake_task_input):
    """(With help off, maxRescues=0: with help on, the Lead breaks the deadlock instead.) Ready work the Lead could not dispatch (held for a file an open item owns) with nothing in flight
    is the same wave again next time: 14 empty waves in 80 s to maxWaves, reported as "max waves
    reached" (run a4f1ff1f). The monitor ends the run at once, naming each hold as a scope_wait
    escalation."""
    from team.tasks import team_monitor
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-scope-starve"
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS[:2], contractPaths=[]))
    tb.set_state(cid, "lead", "alpha", tb.REOPENED)
    held = ["alpha waits for beta to land registry.py"]
    m = team_monitor(fake_task_input(correlationId=cid, maxRescues=0, ceilingUsd=10, dispatchedCount=0, scopeWaits=held)).output_data
    assert m["starved"] is True and m["starvedBy"] == "scope_wait" and "alpha waits for beta" in m["starvedDetail"]
    exc = [e for e in tb.fold_board(cid)["exceptions"] if e["kind"] == "scope_wait"]
    assert exc and exc[-1]["item"] == "alpha" and not exc[-1].get("resolved")
    # Something in flight can still land the file, and a wave that dispatched work moves: not starved.
    tb.set_state(cid, "lead", "beta", tb.IN_PROGRESS)
    assert tb.starvation(tb.fold_board(cid)["items"], [], dispatched=0, held=held)["starved"] is False
    items = {"alpha": {"id": "alpha", "state": tb.REOPENED}, "beta": {"id": "beta", "state": tb.BACKLOG}}
    assert tb.starvation(items, [], dispatched=1, held=held)["starved"] is False
    assert tb.starvation(items, [], dispatched=0, held=[])["starved"] is False


def test_apply_reviews_survives_a_failed_amendment_merge(monkeypatch, tmp_path, fake_task_input):
    """r-writer's approved amendment merge aborted on a git error (an untracked file in the shared
    worktree) and the exception ended the whole resp team as a FAILED task (run a4f1ff1f). A merge
    failure is a decided amendment that did not land: the item is rebuilt against the current head."""
    _isolate_board(monkeypatch, tmp_path)
    from common import blackboard as bbx
    import team.tasks as tt
    cid = "run-amend-fail"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    os.makedirs(os.path.join(shared, "tests"))
    with open(os.path.join(shared, "tests", "test_acc.py"), "w") as fh:
        fh.write("def test_g():\n    assert gamma() == 1\n")
    _commit_all(shared, "acceptance")
    frozen = git.head(shared)
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=["tests/test_acc.py"]))
    bbx.post_fact(cid, "lead", "contract", {"paths": ["tests/test_acc.py"], "frozen": True, "revision": 1,
                                            "acceptanceTests": ["tests/test_acc.py"], "acceptanceCommit": frozen})
    wt = _fork_worktree(shared, "gamma", branch, edit=("tests/test_acc.py", "def test_g():\n    assert gamma() == 2\n"))
    team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="gamma",
        contractPaths=["tests/test_acc.py"], enablePeerReview=True,
        dispatched=[{"id": "gamma", "files": ["gamma.py"], "attempt": 0}],
        joined={"gamma__1": {"output": {"testsPassed": True, "deliveryOutcome": "passed"}}}))

    def failing_merge(*_a, **_k):
        raise RuntimeError("git merge --ff-only cc-group-x-gamma exited 1: untracked working tree files "
                           "would be overwritten by merge: tests/test_acc.py")
    monkeypatch.setattr(tt.continuous_integrate, "integrate_ready", failing_merge)
    out = team_apply_reviews(fake_task_input(
        correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=["gamma"], maxReviewRounds=2,
        joined={"review_gamma": {"output": {"verdict": "lgtm", "findings": []}}})).output_data
    assert out["changes"] == ["gamma"] and out["doneCount"] == 0
    item = tb.fold_board(cid)["items"]["gamma"]
    assert item["state"] == tb.REOPENED and "conflicted on merge" in item["reason"]
    decided = [f["data"] for f in bbx.read_facts(cid, kind="acceptance_amendment") if f["data"].get("decision")]
    assert decided[-1]["decision"] == "reject" and "merge failed" in decided[-1]["reason"]
    assert git.head(shared) == frozen and not os.path.isdir(wt)


def test_integrate_sends_a_satisfied_claim_to_the_peer_reviewer_who_decides(monkeypatch, tmp_path,
                                                                              fake_task_input):
    """cmd-dispatch's implementer found its registry and dispatch already written by cmd-handlers inside
    handlers.go, said so in prose and changed nothing: six repair agents created nothing, the item
    blocked as an empty delivery and starved the server subsystem (run 90e5e3aa). A structured claim is
    judged by the peer reviewer on the current tree: lgtm → done; changes → re-opened with findings."""
    _isolate_board(monkeypatch, tmp_path)
    from common import blackboard as bbx
    cid = "run-claim"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "registry.py"), "w") as fh:
        fh.write("H = {'alpha': 1, 'beta': 2}\n")  # beta's work landed inside alpha's delivery
    _commit_all(shared, "alpha delivered both")
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS[:2], contractPaths=[]))
    tb.set_state(cid, "lead", "alpha", tb.DONE)
    tb.set_state(cid, "lead", "beta", tb.IN_PROGRESS)
    _fork_worktree(shared, "beta", branch)  # no commit
    claim = {"deliveryOutcome": "satisfied", "testsPassed": True,
             "structured": {"summary": "no change", "alreadySatisfied": {"evidence": "registry.py already registers beta"}}}
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="beta", enablePeerReview=True,
        dispatched=[{"id": "beta", "files": ["beta.py", "registry.py"], "attempt": 0}],
        joined={"beta__1": {"output": claim}})).output_data
    assert out["reviewing"] == ["beta"] and out["retryCount"] == 0 and out["blocked"] == []
    assert tb.fold_board(cid)["items"]["beta"]["state"] == tb.REVIEW
    dlv = [f["data"] for f in bbx.read_facts(cid, kind="delivery") if f["data"].get("item") == "beta"][-1]
    assert dlv["claimedSatisfied"] == "registry.py already registers beta"
    assert dlv["base"] == dlv["head"] == git.head(shared)
    rv = team_review_build(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch)).output_data
    prompt = rv["dynamicTasksInput"]["review_beta"]["prompt"]
    assert "MADE NO CHANGE" in prompt and "registry.py already registers beta" in prompt and "(empty diff)" in prompt
    ap = team_apply_reviews(fake_task_input(
        correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=["beta"],
        joined={"review_beta": {"output": {"verdict": "lgtm", "findings": []}}})).output_data
    assert ap["doneCount"] == 1 and tb.fold_board(cid)["items"]["beta"]["state"] == tb.DONE
    # Without a peer reviewer nobody can confirm the claim: it stays an empty delivery, re-queued.
    tb.set_state(cid, "lead", "beta", tb.IN_PROGRESS)
    _fork_worktree(shared, "beta", branch)
    alone = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="beta", enablePeerReview=False,
        dispatched=[{"id": "beta", "files": ["beta.py", "registry.py"], "attempt": 0}],
        joined={"beta__1": {"output": claim}})).output_data
    assert alone["retryCount"] == 1 and alone["reviewing"] == []


def test_apply_reviews_widens_from_the_reviewers_out_of_scope_files_and_counts_no_scope_only_round(
        monkeypatch, tmp_path, fake_task_input):
    """d-store's reviewer named architecture.md (outside the item's files) in round one; the Lead widened
    only after the implementer's refusal a wave later, round two — "not a fault of this implementer,
    the Lead needs to widen" — spent the item's last round, and a new finding in round three
    quarantined it: the program ended at 0/6 (run 0709d705). The reviewer's outOfScope files are now
    widening evidence at once, and a scope-only round counts against no cap."""
    from common import file_ownership
    cid = "run-scope-review"
    shared = _scope_run(monkeypatch, tmp_path, fake_task_input, cid)
    branch = git._current_branch(shared)
    tb.set_state(cid, "lead", "core", tb.DONE)
    tb.set_state(cid, "lead", "lru", tb.REVIEW)
    rv = team_review_build(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch)).output_data
    spec = rv["dynamicTasksInput"]["review_lru"]
    assert "outOfScope" in spec["schema"]["properties"] and "EVERY blocking problem" in spec["prompt"]
    scope_round = {"review_lru": {"output": {"verdict": "changes", "findings": [],
                                             "outOfScope": [{"file": "store.go", "change": "add the eviction hook"}]}}}
    out = team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch,
                                             reviewed=["lru"], maxReviewRounds=2, joined=scope_round)).output_data
    assert out["changes"] == ["lru"] and out["quarantined"] == []
    board = tb.fold_board(cid)
    assert board["items"]["lru"]["state"] == tb.REOPENED and "outside the item's scope" in board["items"]["lru"]["reason"]
    assert board["reviews"][-1]["scopeOnly"] is True
    assert "in store.go: add the eviction hook" in [d for d in board["directives"] if d.get("target") == "lru"][-1]["text"]
    sessions = [f["data"] for f in file_ownership.bb.read_facts(cid, kind="session") if f["data"].get("item") == "lru"]
    assert sessions[-1]["deniedWrites"] == ["store.go"] and sessions[-1]["source"] == "review"
    # The next standup widens lru to store.go from the reviewer's evidence (core, its owner, is done).
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=["contract.go"],
                                      maxWidth=6)).output_data
    assert su["groupIds"] == "lru" and "store.go" in su["dynamicTasksInput"]["lru"]["allowedWriteRoots"]
    # Two scope-only rounds and one real one still leave the item a round: only real findings count.
    tb.set_state(cid, "lead", "lru", tb.REVIEW)
    team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=["lru"],
                                       maxReviewRounds=2, joined=scope_round))
    tb.set_state(cid, "lead", "lru", tb.REVIEW)
    real = team_apply_reviews(fake_task_input(
        correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=["lru"], maxReviewRounds=2,
        joined={"review_lru": {"output": {"verdict": "changes", "findings": ["lru.go: bound the list"]}}})).output_data
    assert real["changes"] == ["lru"] and real["quarantined"] == []
    from common import monitor as mon
    assert mon._review_changes_by_item(tb.fold_board(cid)["reviews"]) == {"lru": 1}


def test_apply_reviews_reopens_an_lgtm_whose_out_of_scope_change_is_still_needed(monkeypatch, tmp_path,
                                                                                  fake_task_input):
    """sv-cmd-registry's reviewer returned lgtm with an outOfScope change (a type declared twice, once in
    a sibling's echo.go); the item was marked done, the change was never applied, and the command
    package stayed unbuildable for the rest of the run (ba2491ff). lgtm with outOfScope is a scope-only
    round: the item is re-opened and widened at the next dispatch, and no cap counts it."""
    cid = "run-lgtm-oos"
    shared = _scope_run(monkeypatch, tmp_path, fake_task_input, cid)
    branch = git._current_branch(shared)
    tb.set_state(cid, "lead", "core", tb.DONE)
    tb.set_state(cid, "lead", "lru", tb.REVIEW)
    out = team_apply_reviews(fake_task_input(
        correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=["lru"], maxReviewRounds=2,
        joined={"review_lru": {"output": {"verdict": "lgtm", "findings": [],
                                          "outOfScope": [{"file": "store.go", "change": "drop the duplicate Handler type"}]}}}
    )).output_data
    assert out["changes"] == ["lru"] and out["doneCount"] == 0
    board = tb.fold_board(cid)
    assert board["items"]["lru"]["state"] == tb.REOPENED
    assert board["reviews"][-1]["verdict"] == "changes" and board["reviews"][-1]["scopeOnly"] is True
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=["contract.go"],
                                      maxWidth=6)).output_data
    assert su["groupIds"] == "lru" and "store.go" in su["dynamicTasksInput"]["lru"]["allowedWriteRoots"]
    assert "in store.go: drop the duplicate Handler type" in su["dynamicTasksInput"]["lru"]["prompt"]


def test_integrate_treats_a_pushback_with_a_refused_edit_as_a_scope_gap(monkeypatch, tmp_path, fake_task_input):
    """sv-cmd-del and sv-cmd-decr were told to make their frozen tests pass on the shared branch, found
    the cause (a type declared twice) in two siblings' files, were refused there, and both blocked as
    constraint_pushback at the attempt cap — the server subsystem ended there (run ba2491ff). A pushback
    whose refused edits lie inside the team's span is a scope gap: the item is re-opened at the same
    attempt, the notice is resolved, and the next standup widens it from the refusal."""
    from common import blackboard as bbx
    cid = "run-scope-push"
    shared = _scope_run(monkeypatch, tmp_path, fake_task_input, cid)
    branch = git._current_branch(shared)
    tb.set_state(cid, "lead", "core", tb.DONE)
    tb.set_state(cid, "lead", "lru", tb.IN_PROGRESS, attempt=1)  # already re-opened once by the check
    text = "Its frozen acceptance test FAILS on the shared branch; make it pass there"
    tb.post_directive(cid, "integrator", "lru", text, authority="agent")
    d1 = tb.fold_board(cid)["directives"][-1]["id"]
    bbx.post_fact(cid, "lead", "dispatch", {"item": "lru", "candidate": "lru",
                                            "directives": [{"id": d1, "text": text, "authority": "agent"}]})
    _fork_worktree(shared, "lru", branch)  # no commit: the fix it needed was refused
    out = team_integrate(fake_task_input(maxRescues=0, 
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="lru", maxAttempts=2,
        dispatched=[{"id": "lru", "files": ["lru.go"], "attempt": 1}],
        joined={"lru__1": {"output": {"deliveryOutcome": "pushback", "deniedWrites": ["store.go"], "structured": {
            "summary": "no change", "pushback": {
                "directive": "make the frozen acceptance test pass on the shared branch",
                "reason": "the build break is a type declared twice in store.go, outside my files"}}}}}
    )).output_data
    assert out["blocked"] == [] and [r["id"] for r in out["retry"]] == ["lru"]
    board = tb.fold_board(cid)
    assert board["items"]["lru"]["state"] == tb.REOPENED and board["items"]["lru"]["attempt"] == 1
    assert "outside the item's scope" in board["items"]["lru"]["reason"]
    assert not tb.open_exceptions(board["exceptions"])  # decided: a resolved scope notice, nothing waits
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=["contract.go"],
                                      maxWidth=6)).output_data
    assert su["groupIds"] == "lru" and "store.go" in su["dynamicTasksInput"]["lru"]["allowedWriteRoots"]
    # A pushback that refused nothing is a refused directive: exempted and re-dispatched at the SAME attempt,
    # never a block or a rescue (an item has only as many directives to refuse as it was given; five items
    # went to stronger-tier helpers for refusing advice that was never theirs, run 351812b2).
    tb.set_state(cid, "lead", "lru", tb.IN_PROGRESS, attempt=1)
    _fork_worktree(shared, "lru", branch)
    plain = team_integrate(fake_task_input(maxRescues=0, 
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="lru", maxAttempts=2,
        dispatched=[{"id": "lru", "files": ["lru.go"], "attempt": 1}],
        joined={"lru__1": {"output": {"deliveryOutcome": "pushback", "deniedWrites": [], "structured": {
            "summary": "no change", "pushback": {
                "directive": "make the frozen acceptance test pass on the shared branch",
                "reason": "the test contradicts the design"}}}}})).output_data
    assert plain["blocked"] == [] and [r["id"] for r in plain["retry"]] == ["lru"]
    assert tb.fold_board(cid)["items"]["lru"]["attempt"] == 1 and "exempted" in tb.fold_board(cid)["items"]["lru"]["reason"]


def test_apply_reviews_at_the_cap_accepts_when_every_prior_finding_was_addressed(monkeypatch, tmp_path,
                                                                                  fake_task_input):
    """The architecture document's reviewer raised three findings (all fixed), then one new finding
    about the fix, then two new findings about that fix: the implementer never left a finding
    unaddressed, the one rank-0 item was quarantined at the cap and the program ended at 0/6 (run
    a57bf979). Over the cap with every prior finding addressed, the Lead accepts the item and carries
    the new findings to its dependents as directives; with prior findings unaddressed, or with no
    'unresolved' report at all, it quarantines as before."""
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-cap-accept"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {"id": "arch", "description": "architecture doc", "files": ["docs/architecture.md"]},
        {"id": "store", "description": "store doc", "files": ["docs/store.md"], "dependsOn": ["arch"]},
        {"id": "resp", "description": "resp doc", "files": ["docs/resp.md"], "dependsOn": ["arch"]}]))
    for n in (1, 2):
        tb.post_review(cid, "reviewer_arch", "arch", "changes", findings=[f"round {n} finding"])
    tb.set_state(cid, "lead", "arch", tb.REVIEW)
    third = {"verdict": "changes", "findings": ["Register's signature is unstated"], "unresolved": []}
    out = team_apply_reviews(fake_task_input(maxRescues=0, 
        correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=["arch"], maxReviewRounds=2,
        joined={"review_arch": {"output": third}})).output_data
    assert out["doneCount"] == 1 and out["quarantined"] == []
    board = tb.fold_board(cid)
    assert board["items"]["arch"]["state"] == tb.DONE and "accepted at the review cap" in board["items"]["arch"]["reason"]
    carried = [d for d in board["directives"] if "Register's signature is unstated" in str(d.get("text"))]
    assert sorted(d["target"] for d in carried) == ["resp", "store"]
    cap = [e for e in board["exceptions"] if e["kind"] == "review_cap"]
    assert cap and cap[-1]["resolved"] is True and "every prior finding addressed" in cap[-1]["detail"]
    assert tb.open_exceptions(board["exceptions"]) == []
    # Prior findings left unaddressed at the cap: quarantined, as before.
    cid2 = "run-cap-quarantine"
    team_plan_init(fake_task_input(correlationId=cid2, contractPaths=[], subtasks=[
        {"id": "arch", "description": "architecture doc", "files": ["docs/architecture.md"]}]))
    for n in (1, 2):
        tb.post_review(cid2, "reviewer_arch", "arch", "changes", findings=[f"round {n} finding"])
    tb.set_state(cid2, "lead", "arch", tb.REVIEW)
    stuck = team_apply_reviews(fake_task_input(maxRescues=0, 
        correlationId=cid2, sharedRepo=shared, sharedBranch=branch, reviewed=["arch"], maxReviewRounds=2,
        joined={"review_arch": {"output": {"verdict": "changes", "findings": ["still wrong"],
                                           "unresolved": ["round 2 finding"]}}})).output_data
    assert stuck["quarantined"] == ["arch"]
    assert "prior findings unaddressed: round 2 finding" in tb.fold_board(cid2)["items"]["arch"]["reason"] or \
        tb.fold_board(cid2)["items"]["arch"]["reason"] == "review_rounds_exhausted"
    # A reviewer that does not report 'unresolved' at all gives the Lead no evidence: quarantined too.
    cid3 = "run-cap-unknown"
    team_plan_init(fake_task_input(correlationId=cid3, contractPaths=[], subtasks=[
        {"id": "arch", "description": "architecture doc", "files": ["docs/architecture.md"]}]))
    for n in (1, 2):
        tb.post_review(cid3, "reviewer_arch", "arch", "changes", findings=[f"round {n} finding"])
    tb.set_state(cid3, "lead", "arch", tb.REVIEW)
    rv = team_review_build(fake_task_input(correlationId=cid3, sharedRepo=shared, sharedBranch=branch)).output_data
    spec = rv["dynamicTasksInput"]["review_arch"]
    assert "unresolved" in spec["schema"]["required"] and "In 'unresolved' copy every finding" in spec["prompt"]
    unknown = team_apply_reviews(fake_task_input(maxRescues=0, 
        correlationId=cid3, sharedRepo=shared, sharedBranch=branch, reviewed=["arch"], maxReviewRounds=2,
        joined={"review_arch": {"output": {"verdict": "changes", "findings": ["new"]}}})).output_data
    assert unknown["quarantined"] == ["arch"]


def test_a_program_team_is_gated_on_its_own_frozen_tests(monkeypatch, tmp_path, fake_task_input):
    """The repository documented no test command, so the pgtype team's final check inferred a bare root
    `pytest`: it ran the parser team's acceptance tests while that team was still building, failed there,
    and never ran pgtype's own (run 037e1cbe). A program's team is gated on its done items' frozen tests
    (testCommands); a standalone team keeps the repository's command."""
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-gate"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS[:1], contractPaths=[]))
    tb.post_item(cid, "lead", {**_ITEMS[0], "acceptanceTests": ["tests/acceptance/test_accept_alpha.py"],
                               "testCommands": [{"argv": ["pytest", "tests/acceptance/test_accept_alpha.py"]}]})
    tb.set_state(cid, "lead", "alpha", tb.DONE)
    branch = git._current_branch(shared)
    child = team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=[],
                                               joined={}, parentCorrelationId="prog-1")).output_data
    assert child["gateCommands"] == [{"argv": ["pytest", "tests/acceptance/test_accept_alpha.py"]}]
    assert child["gateMode"] == "commands" and child["testMode"] == "full"
    alone = team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=[],
                                               joined={})).output_data
    assert alone["gateCommands"] == [] and alone["testMode"] == "full" and alone["gateMode"] == "full"
    import json
    d = json.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "team_build.json")))
    by_ref = {ref: _task(d, ref) for ref in ("loop_init", "outcome", "tests", "tests_fork", "wave_loop", "acceptance_build", "acceptance_gate")}
    assert by_ref["tests"]["inputParameters"]["testCommands"] == "${workflow.variables.teamGateCommands}"
    assert d["variables"]["teamGateCommands"] == []
    loop = _task(d, "wave_loop")
    in_loop = {t["taskReferenceName"]: t for t in loop["loopOver"]}
    assert in_loop["replan"]["inputParameters"]["parentCorrelationId"] == "${workflow.input.parentCorrelationId}"
    assert in_loop["capture_round"]["inputParameters"]["teamGateCommands"] == "${replan.output.gateCommands}"
    assert in_loop["capture_round"]["inputParameters"]["testMode"] == "${replan.output.testMode}"
    tests = next(t for t in d["tasks"] if t["taskReferenceName"] == "tests")
    assert tests["inputParameters"]["testMode"] == "${workflow.variables.testMode}"  # a docs team: targeted, never the full suite


def test_a_reviewer_naming_a_file_without_detail_still_blocks_done(monkeypatch, tmp_path, fake_task_input):
    """lgtm plus an outOfScope entry with an empty change flipped the verdict to changes but left no
    finding, so the item fell through to done and the file was never applied (review of PR 16)."""
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-oos-blank"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=[]))
    tb.set_state(cid, "lead", "gamma", tb.REVIEW)
    out = team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch,
                                             reviewed=["gamma"], maxReviewRounds=2, joined={
        "review_gamma": {"output": {"verdict": "lgtm", "findings": [], "outOfScope": [{"file": "echo.go", "change": ""}]}}})).output_data
    assert out["doneCount"] == 0 and out["changes"] == ["gamma"]
    assert tb.fold_board(cid)["items"]["gamma"]["state"] == tb.REOPENED
    assert any("echo.go" in d["text"] for d in tb.fold_board(cid)["directives"] if d.get("target") == "gamma")


def test_a_refused_fast_forward_reopens_one_item_not_the_wave(monkeypatch, tmp_path, fake_task_input):
    """An untracked file in the shared worktree in the way of a fork's commit made `merge --ff-only`
    raise out of integrate_ready, failing the whole integration task with every claim held."""
    from common import continuous_integrate as ci
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    branch = git._current_branch(shared)
    base = git.head(shared)
    wt_g = _fork_worktree(shared, "gamma", branch, edit=("gamma.py", "G = 1\n"))
    wt_a = _fork_worktree(shared, "alpha", branch, edit=("alpha.py", "A = 1\n"))
    with open(os.path.join(shared, "gamma.py"), "w") as fh:  # untracked, collides with gamma's commit
        fh.write("stale\n")
    out = ci.integrate_ready(shared, branch, [
        {"id": "gamma", "worktree": wt_g, "branch": git.group_branch(shared, "gamma"), "readyAt": 0},
        {"id": "alpha", "worktree": wt_a, "branch": git.group_branch(shared, "alpha"), "readyAt": 1}])
    assert [r["item"] for r in out["integrated"]] == ["alpha"]
    assert [r["item"] for r in out["reopened"]] == ["gamma"] and "fast-forward" in out["reopened"][0]["mechanism"]
    assert git.head(shared) != base and open(os.path.join(shared, "gamma.py")).read() == "stale\n"


def test_a_reused_acceptance_test_gets_the_repositorys_own_command(monkeypatch, tmp_path, fake_task_input):
    """A re-run Go team's reused acceptance test was recorded with `pytest <file>` (review of PR 16)."""
    from team.tasks import team_acceptance_build
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-reuse-go"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    os.makedirs(os.path.join(shared, "tests", "acceptance"))
    with open(os.path.join(shared, "go.mod"), "w") as fh:
        fh.write("module example.com/x\n\ngo 1.22\n")
    with open(os.path.join(shared, "tests", "acceptance", "accept_alpha_test.go"), "w") as fh:
        fh.write("package acceptance\n\nimport \"testing\"\n\nfunc TestA(t *testing.T) {}\n")
    with open(os.path.join(shared, "tests", "acceptance", "accept_gamma.feature"), "w") as fh:
        fh.write("Feature: nothing runs this\n")
    _commit_all(shared, "previous attempt's frozen tests")
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {**_ITEMS[0], "acceptanceCriteria": ["a"]}]))
    ab = team_acceptance_build(fake_task_input(correlationId=cid, repoPath=shared,
                                               acceptanceTestPattern="tests/acceptance/accept_{id}_test.go")).output_data
    assert ab["reused"] == {"alpha": "tests/acceptance/accept_alpha_test.go"}
    cmds = tb.fold_board(cid)["items"]["alpha"]["testCommands"]
    assert cmds and cmds[0]["argv"][0] == "go" and "pytest" not in " ".join(cmds[0]["argv"])
    # No tooling of the repository runs the file: no command, and a visible notice rather than a wrong one.
    cid = "run-reuse-none"
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {**_ITEMS[2], "acceptanceCriteria": ["g"]}]))
    team_acceptance_build(fake_task_input(correlationId=cid, repoPath=shared,
                                          acceptanceTestPattern="tests/acceptance/accept_{id}.feature"))
    assert tb.fold_board(cid)["items"]["gamma"]["testCommands"] == []
    assert [e["kind"] for e in tb.open_exceptions(tb.fold_board(cid)["exceptions"])] == ["acceptance_command"]


def test_a_re_run_team_reuses_the_frozen_tests_already_on_the_branch(monkeypatch, tmp_path, fake_task_input):
    """A re-run team found its predecessor's acceptance tests on the branch. Re-authored, they would pass
    at once (the code exists), be judged tautological and be deleted; they are reused as the check."""
    from team.tasks import team_acceptance_build, _frozen_acceptance
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-reuse"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    os.makedirs(os.path.join(shared, "tests", "acceptance"))
    with open(os.path.join(shared, "tests", "acceptance", "test_accept_alpha.py"), "w") as fh:
        fh.write("def test_a():\n    assert True\n")
    _commit_all(shared, "previous attempt's frozen test")
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {**_ITEMS[0], "acceptanceCriteria": ["a"]}, {**_ITEMS[2], "acceptanceCriteria": ["g"]}]))
    ab = team_acceptance_build(fake_task_input(correlationId=cid, repoPath=shared)).output_data
    assert ab["reused"] == {"alpha": "tests/acceptance/test_accept_alpha.py"}
    assert list(ab["authored"]) == ["gamma"]  # only the item without a test gets an author
    item = tb.fold_board(cid)["items"]["alpha"]
    assert item["acceptanceTests"] == ["tests/acceptance/test_accept_alpha.py"]
    assert item["testCommands"] == [{"argv": ["pytest", "tests/acceptance/test_accept_alpha.py"]}]
    assert _frozen_acceptance(cid)["acceptanceTests"] == ["tests/acceptance/test_accept_alpha.py"]


# ───────────── help for a struggling agent: a stronger teammate with the full history ─────────────
def test_a_stuck_item_gets_a_stronger_teammate_with_its_history_before_it_ever_blocks(monkeypatch, tmp_path,
                                                                                       fake_task_input):
    """A swarm helps the agent that is struggling. An item that exhausted its attempts used to block (and
    could starve everything downstream). Now the Lead reopens it, and the next standup hands it to the next
    model tier with everything it went through, and help keeps coming until the work lands."""
    from team.tasks import _item_history
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-help"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=[]))
    tb.post_review(cid, "reviewer_gamma", "gamma", "changes", findings=["gamma() must return 1, it returns 0"])

    def empty_wave(attempt):
        tb.set_state(cid, "lead", "gamma", tb.IN_PROGRESS, attempt=attempt)
        _fork_worktree(shared, "gamma", branch)  # no commit
        return team_integrate(fake_task_input(
            sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="gamma", maxAttempts=2, maxRescues=2,
            dispatched=[{"id": "gamma", "files": ["gamma.py"], "attempt": attempt}],
            joined={"gamma__1": {"output": {"deliveryOutcome": "incomplete_delivery"}}})).output_data

    assert empty_wave(0)["retryCount"] == 1                      # ordinary retry first
    out = empty_wave(1)                                           # attempts exhausted: help, not a block
    assert out["rescued"] == ["gamma"] and out["blocked"] == []
    item = tb.fold_board(cid)["items"]["gamma"]
    assert item["state"] == tb.REOPENED and item["attempt"] == 0 and "stronger teammate" in item["reason"]
    assert tb.open_exceptions(tb.fold_board(cid)["exceptions"]) == []
    history = _item_history(cid, "gamma")
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, maxWidth=6)).output_data
    spec = su["dynamicTasksInput"]["gamma"]
    assert spec["modelTier"] == 1                                 # the next tier up
    assert spec["prompt"].startswith("YOU ARE THE HELP FOR A STUCK WORK ITEM")
    assert "gamma() must return 1" in history and "empty_delivery" in history and history in spec["prompt"]
    # A second helper runs a tier higher still, and help does not stop at maxRescues.
    empty_wave(0); assert empty_wave(1)["rescued"] == ["gamma"]
    assert team_standup(fake_task_input(repoPath=shared, correlationId=cid, maxWidth=6, maxRescues=2)).output_data[
        "dynamicTasksInput"]["gamma"]["modelTier"] == 2
    empty_wave(0)
    final = empty_wave(1)
    assert final["rescued"] == ["gamma"] and final["blocked"] == []
    assert tb.fold_board(cid)["items"]["gamma"]["state"] == tb.REOPENED
    # maxRescues is how many tiers up help may climb: the third helper stays at the strongest reached.
    assert team_standup(fake_task_input(repoPath=shared, correlationId=cid, maxWidth=6, maxRescues=2)).output_data[
        "dynamicTasksInput"]["gamma"]["modelTier"] == 2


def test_help_for_a_stuck_item_may_change_anything(monkeypatch, tmp_path, fake_task_input):
    """foundation's fix sat in internal/store/zsets.go, outside its team's directories: both helpers were
    refused there and the item blocked, starving the team (run 41fddf7a). A helper is unscoped, is never
    held or refused, and its reviewer reads the whole diff."""
    _isolate_board(monkeypatch, tmp_path)
    from common import blackboard as bbx
    cid = "run-help-scope"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=[]))
    bbx.post_fact(cid, "integrator", "session", {"item": "gamma", "candidate": "gamma",
                                                 "deniedWrites": ["other/subsystem/store.go"]})
    # Before any help the old scope rules stand.
    tb.set_state(cid, "lead", "gamma", tb.REOPENED)
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, maxWidth=6)).output_data
    assert su["dynamicTasksInput"]["gamma"]["allowedWriteRoots"] == ["gamma.py"]
    tb.set_state(cid, "lead", "gamma", tb.IN_PROGRESS)

    _fork_worktree(shared, "gamma", branch)  # no commit
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="gamma", maxAttempts=1, maxRescues=2,
        dispatched=[{"id": "gamma", "files": ["gamma.py"], "attempt": 0}],
        joined={"gamma__1": {"output": {"deliveryOutcome": "incomplete_delivery"}}})).output_data
    assert out["rescued"] == ["gamma"]
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, maxWidth=6)).output_data
    spec = su["dynamicTasksInput"]["gamma"]
    assert spec["allowedWriteRoots"] == [] and "You may change ANY file in the repository" in spec["prompt"]
    assert su["scopeRefusals"] == [] and su["scopeWaits"] == []

    wt = _fork_worktree(shared, "gamma", branch, edit=("elsewhere.py", "fixed = True\n"))
    assert os.path.isdir(wt)
    team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="gamma", maxAttempts=2, maxRescues=2,
        enablePeerReview=True, dispatched=[{"id": "gamma", "files": ["gamma.py"], "attempt": 0}],
        joined={"gamma__1": {"output": {"testsPassed": True, "deliveryOutcome": "passed"}}}))
    assert open(os.path.join(shared, "elsewhere.py")).read() == "fixed = True\n"
    rv = team_review_build(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch)).output_data
    assert "elsewhere.py" in rv["dynamicTasksInput"]["review_gamma"]["prompt"]


def test_without_a_reviewer_the_lead_merges_a_helpers_contract_correction(monkeypatch, tmp_path, fake_task_input):
    """With peer review off nobody could judge the correction its prompt invites; the Lead merges it instead
    of blocking the item as a pending contract change and looping helper after helper."""
    _isolate_board(monkeypatch, tmp_path)
    from common import blackboard as bbx
    cid = "run-help-contract-noreview"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "api.py"), "w") as fh:
        fh.write("V = 1\n")
    _commit_all(shared, "contract")
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=["api.py"]))
    bbx.post_fact(cid, "lead", "contract", {"paths": ["api.py"], "frozen": True, "revision": 1})
    bbx.post_fact(cid, "lead", "rescue", {"item": "gamma", "rescue": 1, "mechanism": "stuck"})
    _fork_worktree(shared, "gamma", branch, edit=("api.py", "V = 2\n"))
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="gamma", contractPaths=["api.py"],
        enablePeerReview=False, dispatched=[{"id": "gamma", "files": ["gamma.py"], "attempt": 0}],
        joined={"gamma__1": {"output": {"testsPassed": True, "deliveryOutcome": "passed"}}})).output_data
    assert out["doneNow"] == ["gamma"] and out["blocked"] == []
    assert open(os.path.join(shared, "api.py")).read() == "V = 2\n"
    decided = [f["data"] for f in bbx.read_facts(cid, kind="acceptance_amendment") if f["data"].get("decision")]
    assert decided[-1]["decision"] == "approve" and "no peer review" in decided[-1]["reason"]
    assert tb.fold_board(cid)["items"]["gamma"]["state"] == tb.DONE


def test_a_helper_may_propose_changing_the_frozen_contract_for_review(monkeypatch, tmp_path, fake_task_input):
    """A frozen contract that is genuinely wrong would otherwise reject every helper's fix for good."""
    _isolate_board(monkeypatch, tmp_path)
    from common import blackboard as bbx
    cid = "run-help-contract"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "api.py"), "w") as fh:
        fh.write("V = 1\n")
    _commit_all(shared, "contract")
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=["api.py"]))
    bbx.post_fact(cid, "lead", "contract", {"paths": ["api.py"], "frozen": True, "revision": 1})
    bbx.post_fact(cid, "lead", "rescue", {"item": "gamma", "rescue": 1, "mechanism": "stuck"})
    wt = _fork_worktree(shared, "gamma", branch, edit=("api.py", "V = 2\n"))
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, groupIds="gamma", contractPaths=["api.py"],
        enablePeerReview=True, dispatched=[{"id": "gamma", "files": ["gamma.py"], "attempt": 0}],
        joined={"gamma__1": {"output": {"testsPassed": True, "deliveryOutcome": "passed"}}})).output_data
    assert out["reviewing"] == ["gamma"] and os.path.isdir(wt)
    assert tb.fold_board(cid)["items"]["gamma"]["state"] == tb.REVIEW
    assert open(os.path.join(shared, "api.py")).read() == "V = 1\n"  # held unmerged until the reviewer decides


def test_review_rounds_restart_for_the_helper_and_a_silent_reviewer_is_escalated(monkeypatch, tmp_path,
                                                                                  fake_task_input):
    """Over the review cap with findings unaddressed, the item gets a helper instead of quarantine, and the
    cap counts the helper's own rounds. A reviewer that returned no verdict re-reviews a tier up."""
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-help-review"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[_ITEMS[2]], contractPaths=[]))
    for n in (1, 2):
        tb.post_review(cid, "reviewer_gamma", "gamma", "changes", findings=[f"round {n}"])
    tb.set_state(cid, "lead", "gamma", tb.REVIEW)
    stuck = {"review_gamma": {"output": {"verdict": "changes", "findings": ["still wrong"], "unresolved": ["round 2"]}}}
    out = team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch,
                                             reviewed=["gamma"], maxReviewRounds=2, maxRescues=1, joined=stuck)).output_data
    assert out["rescued"] == ["gamma"] and out["quarantined"] == []
    assert tb.fold_board(cid)["items"]["gamma"]["state"] == tb.REOPENED
    assert any("still wrong" in d["text"] for d in tb.fold_board(cid)["directives"] if d.get("target") == "gamma")
    # The helper's first change round is round 1 of its own, not round 4.
    tb.set_state(cid, "lead", "gamma", tb.REVIEW)
    again = team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch,
                                               reviewed=["gamma"], maxReviewRounds=2, maxRescues=1, joined={
        "review_gamma": {"output": {"verdict": "changes", "findings": ["one more"], "unresolved": []}}})).output_data
    assert again["changes"] == ["gamma"] and again["quarantined"] == [] and again["rescued"] == []
    # A silent reviewer: its re-review runs one tier up.
    tb.post_review(cid, "reviewer_gamma", "gamma", "unavailable")
    tb.set_state(cid, "lead", "gamma", tb.REVIEW)
    rv = team_review_build(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch)).output_data
    assert rv["dynamicTasksInput"]["review_gamma"]["modelTier"] == 1


def test_model_tier_escalates_within_the_roles_tiers():
    from common.model_policy import select_role_tier
    res = {"roles": {"code": {"tiers": [{"agent": "claude", "model": "claude-sonnet-5"},
                                        {"agent": "claude", "model": "claude-opus-4-8"}]}}}
    pick = lambda **kw: select_role_tier({"modelResolution": res, **kw}, role="code")[0]["model"]  # noqa: E731
    assert pick() == "claude-sonnet-5" and pick(modelTier=1) == "claude-opus-4-8"
    assert pick(modelTier=5) == "claude-opus-4-8" and pick(modelTier="x") == "claude-sonnet-5"
    import json
    d = json.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "code_subtask.json")))
    assert d["inputTemplate"]["modelTier"] == 0
    t = json.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "team_build.json")))
    assert t["inputTemplate"]["maxRescues"] == 2 and t["inputTemplate"]["baseModelTier"] == 0


def test_apply_reviews_accepts_an_item_whose_only_findings_are_outside_the_teams_directories(
        monkeypatch, tmp_path, fake_task_input):
    """storage-core's second review asked only for docs/architecture.md, a file outside the store team's
    directories that the standup can never widen to: the item was re-dispatched with a directive it had
    to push back on, the Lead exempted it, and a fourth round began for nothing (run c4a9237d). Such a
    round accepts the item and carries the change to the team as a directive."""
    cid = "run-scope-unreachable"
    shared = _scope_run(monkeypatch, tmp_path, fake_task_input, cid)
    branch = git._current_branch(shared)
    tb.set_state(cid, "lead", "core", tb.DONE)
    tb.set_state(cid, "lead", "lru", tb.REVIEW)
    docs_round = {"review_lru": {"output": {"verdict": "changes", "findings": [],
                                            "outOfScope": [{"file": "docs/architecture.md",
                                                            "change": "describe the eviction hook"}]}}}
    out = team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch,
                                             reviewed=["lru"], maxReviewRounds=2, joined=docs_round)).output_data
    assert out["changes"] == [] and "lru" in out["doneCount"] if isinstance(out["doneCount"], list) else out["doneCount"] >= 1
    board = tb.fold_board(cid)
    assert board["items"]["lru"]["state"] == tb.DONE and "outside this team's directories" in board["items"]["lru"]["reason"]
    carried = [d for d in board["directives"] if d.get("target") == "team"][-1]["text"]
    assert "docs/architecture.md" in carried and "describe the eviction hook" in carried
    assert not [d for d in board["directives"] if d.get("target") == "lru"]  # nothing was sent back to the implementer
    assert [e["kind"] for e in board["exceptions"] if e.get("item") == "lru"][-1] == "scope_unreachable"


def test_acceptance_test_that_passes_over_existing_files_is_kept_as_the_gate(monkeypatch, tmp_path, fake_task_input):
    """storage-core's freshly written test passed at the red run because the repository already held the
    store: the test was discarded as tautological, the item had no gate, and every fork spent four
    minutes in the discovery fallback (run c4a9237d). Over existing files the green test is the gate;
    over files that do not exist yet it is still a tautology."""
    from team.tasks import team_acceptance_build, team_acceptance_commit
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-green-gate"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    for name in ("registry.py", "alpha.py"):
        with open(os.path.join(shared, name), "w") as fh:
            fh.write("x = 1\n")
    _commit_all(shared, "base")
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS, contractPaths=[]))
    tb.post_item(cid, "lead", {**_ITEMS[1], "acceptanceCriteria": ["beta registered"]})
    ab = team_acceptance_build(fake_task_input(correlationId=cid, repoPath=shared)).output_data
    os.makedirs(os.path.join(shared, "tests", "acceptance"))
    for p in ab["authored"].values():
        with open(os.path.join(shared, p), "w") as fh:
            fh.write("def test_x():\n    assert True\n")
    _commit_all(shared, "acceptance")
    run = {"verificationState": "passed", "commands": [
        {"argv": ["pytest", "-q", "tests/acceptance/test_accept_alpha.py"], "exitCode": 0},
        {"argv": ["pytest", "-q", "tests/acceptance/test_accept_beta.py"], "exitCode": 0}]}
    out = team_acceptance_commit(fake_task_input(correlationId=cid, repoPath=shared, authored=ab["authored"], run=run)).output_data
    # alpha's files exist: kept, flagged; beta's beta.py does not: discarded as before.
    assert list(out["kept"]) == ["alpha"] and out["greenBeforeWork"] == ["alpha"] and out["discarded"] == ["beta"]
    item = tb.fold_board(cid)["items"]["alpha"]
    assert item["greenBeforeWork"] is True and item["testCommands"] == [{"argv": ["pytest", "-q", "tests/acceptance/test_accept_alpha.py"]}]
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=[], maxWidth=6)).output_data
    spec = su["dynamicTasksInput"]["alpha"]
    assert "ALREADY PASSES" in spec["prompt"] and spec["testCommands"] == item["testCommands"]
    assert su["dynamicTasksInput"]["gamma"]["testCommands"] == []


def test_standup_widens_into_a_delivered_subsystems_file_and_refuses_an_open_ones(monkeypatch, tmp_path, fake_task_input):
    """ce-zset-inf-scores needed one line in internal/store/zsets.go; its team spanned internal/command,
    the standup refused to widen ("the program's call") three rounds running, and the gap waited for the
    final repair although the store team had delivered long before (run 351812b2). A file no open
    subsystem of the program still owns is widened to; one a subsystem is still building stays refused."""
    from common import file_ownership
    _isolate_board(monkeypatch, tmp_path)
    parent, cid = "prog-widen", "team-widen"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    for p in ("cmd/z.go", "store/zsets.go", "net/server.go"):
        os.makedirs(os.path.join(shared, os.path.dirname(p)), exist_ok=True)
        with open(os.path.join(shared, p), "w") as fh:
            fh.write("package x\n")
    _commit_all(shared, "base")
    # The program board: the store subsystem is delivered, the server subsystem is still being built.
    tb.post_item(parent, "program", {"id": "store", "files": ["store/zsets.go"], "kind": "subsystem"})
    tb.set_state(parent, "program", "store", tb.DONE)
    tb.post_item(parent, "program", {"id": "server", "files": ["net/server.go"], "kind": "subsystem"})
    tb.set_state(parent, "program", "server", tb.IN_PROGRESS)
    tb.post_item(parent, "program", {"id": "cmd", "files": ["cmd/z.go"], "kind": "subsystem"})
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[{"id": "zinf", "description": "inf scores", "files": ["cmd/z.go"]}],
                                   contractPaths=[], parentCorrelationId=parent, subsystemId="cmd"))
    tb.set_state(cid, "lead", "zinf", tb.REOPENED, attempt=1)
    file_ownership.bb.post_fact(cid, "integrator", "session", {"item": "zinf", "deniedWrites": ["store/zsets.go", "net/server.go"]})
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, contractPaths=[], maxWidth=6,
                                      parentCorrelationId=parent, subsystemId="cmd")).output_data
    files = su["dynamicTasksInput"]["zinf"]["allowedWriteRoots"]
    assert "store/zsets.go" in files and "net/server.go" not in files
    assert any("net/server.go" in r and "still being built (server)" in r for r in su["scopeRefusals"])
    assert not any("store/zsets.go" in r for r in su["scopeRefusals"])


def test_acceptance_tests_are_authored_beside_the_first_wave(monkeypatch, tmp_path, fake_task_input):
    """Authoring cost every team 3-6 minutes before any implementer started (run 351812b2: 2.6-6.1 min
    to the first standup). The authors now run beside the wave loop; a wave that would drain before the
    tests have landed waits for them on the board (never for a human), so the integration check can run
    the frozen tests at the integrated head before the team calls itself done."""
    import json
    import threading
    from team.tasks import ACCEPTANCE_SETTLED_KIND, team_acceptance_build
    from common import blackboard as bbx
    _isolate_board(monkeypatch, tmp_path)
    d = json.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "team_build.json")))
    # Authored BESIDE the first wave (again): serialized before it, two authors held the first implementer
    # for 142 s on a two-item team (run e8bc7c2a). The Architect's own scenario now pins the interface from
    # the first turn, so a test landing mid-wave rarely disagrees with the build; when it does, the
    # integration check runs it at the head and re-opens the item, and the loop waits for the tests to
    # settle before it drains. Up to maxWidth implementers and the authors run concurrently.
    order = [t["taskReferenceName"] for t in d["tasks"]]
    # No author chain in the workflow any more: plan_init settles the acceptance tests on the board and the
    # wave loop starts at once; the gate is refreshed from the board after the loop.
    assert "accept_and_build" not in order and order.index("wave_loop") < order.index("gate_refresh")
    # Nothing to author (no criteria): the settle fact is posted at once.
    cid = "run-settle-skip"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=[{"id": "a", "files": ["a.py"]}], contractPaths=[]))
    assert team_acceptance_build(fake_task_input(correlationId=cid, repoPath=shared)).output_data["gate"] == "skip"
    assert bbx.read_facts(cid, kind=ACCEPTANCE_SETTLED_KIND)
    # A drained board never waits on anything: the tests are on the branch before the first standup.
    cid2 = "run-settle-wait"
    team_plan_init(fake_task_input(correlationId=cid2, subtasks=[{"id": "a", "files": ["a.py"]}], contractPaths=[]))
    tb.set_state(cid2, "lead", "a", tb.DONE)
    import time as _t
    t0 = _t.time()
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid2, contractPaths=[], maxWidth=6,
                                      authorAcceptanceTests=True)).output_data
    assert su["dispatchedCount"] == 0 and _t.time() - t0 < 2.5
    del threading


def test_final_gate_is_refreshed_after_the_acceptance_and_build_branches_join(monkeypatch, tmp_path, fake_task_input):
    """Frozen tests authored beside the first wave can land after the last wave's replan captured the
    gate: the calculator team's item was done at 27 s, its test committed at 72 s, the final cycle ran
    no command for it and the head on record predated the test's own commit, so the delivered head
    was 'unproven' and the frozen test 'modified after frozen' (run 884f7eec). The gate the final cycle
    runs is read from the board once both branches have joined, and the verdict reads the live head."""
    import json
    from team.tasks import team_gate_refresh, team_outcome
    _isolate_board(monkeypatch, tmp_path)
    d = json.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "team_build.json")))
    order = [t["taskReferenceName"] for t in d["tasks"]]
    assert order.index("wave_loop") < order.index("gate_refresh") < order.index("capture_gate") < order.index("tests_fork")
    cap = _task(d, "capture_gate")["inputParameters"]
    assert cap == {"finalHead": "${gate_refresh.output.finalHead}", "acceptanceCommands": "${gate_refresh.output.acceptanceCommands}",
                   "testWriteRoots": "${gate_refresh.output.writeRoots}", "teamGateCommands": "${gate_refresh.output.gateCommands}",
                   "testMode": "${gate_refresh.output.testMode}"}
    assert _task(d, "gate_refresh")["inputParameters"]["parentCorrelationId"] == "${workflow.input.parentCorrelationId}"
    cid = "run-gate-late"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS[:1], contractPaths=[]))
    tb.set_state(cid, "lead", "alpha", tb.DONE)
    # The frozen test lands after the item was done (the authoring branch finishing second).
    tb.post_item(cid, "lead", {**_ITEMS[0], "acceptanceTests": ["tests/acceptance/test_accept_alpha.py"],
                               "testCommands": [{"argv": ["pytest", "tests/acceptance/test_accept_alpha.py"]}]})
    os.makedirs(os.path.join(shared, "tests/acceptance"), exist_ok=True)
    open(os.path.join(shared, "tests/acceptance/test_accept_alpha.py"), "w").write("def test_a(): pass\n")
    git.commit(shared, "frozen", force_add_paths=["tests/acceptance/test_accept_alpha.py"])
    late_head = git.head(shared)
    from common import blackboard as bbx
    bbx.post_fact(cid, "lead", "contract", {"paths": ["tests/acceptance/test_accept_alpha.py"], "frozen": True, "revision": 1,
                                            "acceptanceTests": ["tests/acceptance/test_accept_alpha.py"], "acceptanceCommit": late_head})
    gate = team_gate_refresh(fake_task_input(correlationId=cid, sharedRepo=shared, parentCorrelationId="prog-1")).output_data
    assert gate["finalHead"] == late_head
    assert gate["gateCommands"] == [{"argv": ["pytest", "tests/acceptance/test_accept_alpha.py"]}]
    assert gate["gateMode"] == "commands" and gate["testMode"] == "full"
    assert gate["writeRoots"] == sorted(_ITEMS[0]["files"])
    # The outcome judges the branch as it is, not the head a wave recorded before the test landed.
    out = team_outcome(fake_task_input(correlationId=cid, finalHead="0" * 40, testsPassed=True, testCycleState="green",
                                       testCommandsRan=[{"argv": ["pytest", "tests/acceptance/test_accept_alpha.py"]}],
                                       repoPath=shared, sharedRepo=shared, sharedBranch=git._current_branch(shared))).output_data
    assert out["finalHead"] == late_head and out["acceptanceTampered"] == [] and out["acceptanceUnproven"] == []


def test_the_integration_check_covers_only_what_the_wave_touched(monkeypatch, tmp_path):
    """Every integrated item's frozen tests ran at every wave: test time grew with the square of the
    item count. A wave checks the items it landed, the items whose files or frozen test its diff
    touched, and the dependants of a landed item; with no wave delta it checks everything."""
    from team.tasks import _integration_check
    from common import verification
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    for f in ("a.py", "b.py", "c.py"):
        open(os.path.join(shared, f), "w").write("x = 1\n")
    _commit_all(shared, "base")
    before = git.head(shared)
    open(os.path.join(shared, "b.py"), "w").write("x = 2\n")
    _commit_all(shared, "wave")
    head = git.head(shared)
    cmd = lambda p: [{"argv": ["pytest", "-q", p]}]  # noqa: E731
    items = {"a": {"state": tb.DONE, "files": ["a.py"], "acceptanceTests": ["tests/test_a.py"], "testCommands": cmd("tests/test_a.py")},
             "b": {"state": tb.DONE, "files": ["b.py"], "acceptanceTests": ["tests/test_b.py"], "testCommands": cmd("tests/test_b.py")},
             "c": {"state": tb.DONE, "files": ["c.py"], "acceptanceTests": ["tests/test_c.py"], "testCommands": cmd("tests/test_c.py"), "dependsOn": ["d"]},
             "d": {"state": tb.DONE, "files": ["d.py"], "acceptanceTests": ["tests/test_d.py"], "testCommands": cmd("tests/test_d.py")}}
    seen = []
    monkeypatch.setattr(verification, "verify_candidate",
                        lambda repo, h, commands, **k: seen.append(sorted(" ".join(c["argv"]) for c in commands))
                        or {"verificationState": "passed", "commands": [{**c, "exitCode": 0} for c in commands]})
    out = _integration_check(None, shared, head, items, sandbox=False, since=before, landed=["d"])
    # d landed; b's file changed in the wave; c depends on d; a is untouched.
    assert out["checked"] == ["b", "c", "d"] and seen[-1] == ["pytest -q tests/test_b.py", "pytest -q tests/test_c.py", "pytest -q tests/test_d.py"]
    out = _integration_check(None, shared, head, items, sandbox=False)
    assert out["checked"] == ["a", "b", "c", "d"]


def test_a_programs_only_open_team_runs_the_repositorys_own_command(monkeypatch, tmp_path, fake_task_input):
    """A program's team with no frozen test ran its final cycle `targeted`; targeted discovery cannot map
    a new project's package.json to a unit, so the one-item React team fell back to an agent that proposed
    nothing and then authored a test it did not need (run b00146a9). With no sibling subsystem still open
    there is nothing of anyone else's for the full suite to run: the team keeps the repository's command."""
    _isolate_board(monkeypatch, tmp_path)
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    branch = git._current_branch(shared)
    tb.post_item("prog-only", "program", {"id": "calc", "kind": "subsystem", "files": ["a.py"]})
    cid = "run-only-team"
    team_plan_init(fake_task_input(correlationId=cid, subtasks=_ITEMS[:1], contractPaths=[], parentCorrelationId="prog-only",
                                   subsystemId="calc"))
    tb.set_state(cid, "lead", "alpha", tb.DONE)
    only = team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=[], joined={},
                                              parentCorrelationId="prog-only", subsystemId="calc")).output_data
    assert only["gateCommands"] == [] and only["gateMode"] == "full" and only["testMode"] == "full"
    # A sibling still building: targeted, never the whole suite (its tests would run too).
    tb.post_item("prog-only", "program", {"id": "api", "kind": "subsystem", "files": ["b.py"]})
    tb.set_state("prog-only", "program", "api", tb.IN_PROGRESS)
    sib = team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=[], joined={},
                                             parentCorrelationId="prog-only", subsystemId="calc")).output_data
    assert sib["gateMode"] == "targeted" and sib["testMode"] == "targeted"
    import json
    d = json.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "team_build.json")))
    for ref in ("gate_refresh", "replan"):
        assert _task(d, ref)["inputParameters"]["subsystemId"] == "${workflow.input.subsystemId}"


# ─────────────────────────── ONE tree per team: integration records, never rebases ───────────────
def _shared_team(monkeypatch, tmp_path, fake_task_input, cid):
    _isolate_board(monkeypatch, tmp_path)
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {"id": "alpha", "description": "add alpha", "files": ["alpha.py"]},
        {"id": "beta", "description": "add beta", "files": ["beta.py"]}]))
    from common import file_ownership
    file_ownership.bb.post_fact(cid, "lead", "dispatch", {"item": "alpha", "waveBase": git.head(shared)})
    return shared, branch


def test_a_shared_tree_item_is_integrated_where_it_committed_while_a_teammate_is_mid_edit(
        monkeypatch, tmp_path, fake_task_input):
    """Run 836437d7: the integrator rebased the team's ONE tree onto its own branch; git refused on a
    teammate's uncommitted file and five delivered items were re-opened as conflicts with no file."""
    cid = "run-shared"
    shared, branch = _shared_team(monkeypatch, tmp_path, fake_task_input, cid)
    base = git.head(shared)
    with open(os.path.join(shared, "alpha.py"), "w") as fh:
        fh.write("A = 1\n")
    git.git(shared, "add", "alpha.py")
    git.git(shared, "commit", "-m", "code_subtask alpha")
    with open(os.path.join(shared, "beta.py"), "w") as fh:  # the teammate, still working
        fh.write("B = 1\n")
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 2\n")
    head = git.head(shared)
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, contractPaths=[], groupIds="alpha",
        dispatched=[{"id": "alpha", "files": ["alpha.py"], "attempt": 0}], enablePeerReview=True,
        joined={"alpha": {"output": {"deliveryOutcome": "passed", "commit": head, "filesChanged": ["alpha.py"],
                                     "stagedPaths": ["alpha.py"], "presentPaths": []}}})).output_data
    assert out["reviewing"] == ["alpha"] and out["retry"] == [] and out["blocked"] == []
    assert git.head(shared) == head, "integration moved the shared branch"
    assert git.git(shared, "status", "--porcelain").stdout.count("\n") == 2, "the teammate's edits were touched"
    from common import file_ownership
    delivery = [f["data"] for f in file_ownership.bb.read_facts(cid, kind="delivery")][-1]
    assert (delivery["base"], delivery["head"]) == (base, head)


def test_a_re_dispatched_shared_tree_item_whose_files_are_already_committed_is_delivered(
        monkeypatch, tmp_path, fake_task_input):
    """Run 836437d7: a re-dispatched item had nothing left to commit, its audit said passed (its planned
    files are at HEAD), and the integrator called it an empty delivery eight times in a row."""
    cid = "run-redo"
    shared, branch = _shared_team(monkeypatch, tmp_path, fake_task_input, cid)
    with open(os.path.join(shared, "alpha.py"), "w") as fh:
        fh.write("A = 1\n")
    _commit_all(shared, "code_subtask alpha")
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, contractPaths=[], groupIds="alpha",
        dispatched=[{"id": "alpha", "files": ["alpha.py"], "attempt": 1}], enablePeerReview=True,
        joined={"alpha": {"output": {"deliveryOutcome": "passed", "commit": git.head(shared), "filesChanged": [],
                                     "stagedPaths": [], "presentPaths": ["alpha.py"]}}})).output_data
    assert out["reviewing"] == ["alpha"] and out["retry"] == []


def test_a_shared_tree_item_whose_audit_failed_is_still_not_delivered(monkeypatch, tmp_path, fake_task_input):
    cid = "run-short"
    shared, branch = _shared_team(monkeypatch, tmp_path, fake_task_input, cid)
    out = team_integrate(fake_task_input(maxRescues=0,
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, contractPaths=[], groupIds="alpha",
        dispatched=[{"id": "alpha", "files": ["alpha.py"], "attempt": 0}], enablePeerReview=True, maxAttempts=2,
        joined={"alpha": {"output": {"deliveryOutcome": "incomplete_delivery", "commit": git.head(shared),
                                     "filesChanged": [], "stagedPaths": [], "presentPaths": [],
                                     "agentCompleted": True}}})).output_data
    assert out["reviewing"] == [] and len(out["retry"]) == 1


def test_a_rebase_git_refuses_names_gits_reason_not_a_conflict(tmp_path):
    from common import continuous_integrate as ci
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit_all(shared, "base")
    branch = git._current_branch(shared)
    wt = _fork_worktree(shared, "alpha", branch, edit=("alpha.py", "A = 1\n"))
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 3\n")
    _commit_all(shared, "moved")
    with open(os.path.join(wt, "seed.py"), "w") as fh:  # uncommitted edit in the fork
        fh.write("x = 2\n")
    out = ci.integrate_ready(shared, branch, [{"id": "alpha", "worktree": wt, "branch": git.group_branch(shared, "alpha")}])
    rec = out["reopened"][0]
    assert "none reported" not in rec["mechanism"] and "refused" in rec["mechanism"]
