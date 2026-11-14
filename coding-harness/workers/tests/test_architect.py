"""The Architect: the definition of done, its score, the convergence ladder, steer, and the worker
tasks that run checks, fold evaluations and end a program only as delivered or with named needs."""
import json
import os
import stat

import common  # noqa: F401 -- sys.path shim
from common import architect as a, git, team_board as tb
from common import blackboard as bb
from architect.tasks import (architect_fold, contract_fold, contract_of, contract_review_fold, contract_run,
                             evaluations_of, opinion_fold, check_scout_build, results_of, run_check, board_view)
from stages.tasks import intake_route, program_apply_rank, program_outcome, program_rank_init


def _iso(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))


def _contract(**kw):
    c = a.fold_contract({"threshold": 1.0}, [], {}, external=False)
    c.update(kw)
    return c


def _red_acceptance(cid):
    """A child team whose one done item carries a frozen test that fails: the program's acceptance
    check runs it and is red (with no frozen test at all it would hold vacuously once every team is done)."""
    bb.post_fact(cid, "child-x", "child", {"subsystem": "srv", "childRun": f"child-{cid}"})
    tb.post_item(f"child-{cid}", "lead", {"id": "srv-item", "files": ["app.py"],
                                            "testCommands": [{"argv": ["sh", "-c", "exit 1"]}]})
    tb.set_state(f"child-{cid}", "lead", "srv-item", tb.DONE)


def _results(**states):
    return {k: {"state": v} for k, v in states.items()}


# ─────────────────────────── the draft and the scouts ─────────────────────────
def test_draft_problems_reject_placeholders_like_the_decomposition_gate():
    assert a.draft_problems({}) == []
    assert a.draft_problems({"threshold": 1.0, "checks": [{"kind": "client", "claim": "redis-py SET/GET pass"}]}) == []
    probs = a.draft_problems({"threshold": 2, "checks": [{"kind": "client", "claim": "ok"}, {"kind": "x", "claim": "real"}]})
    assert any("threshold" in p for p in probs) and any("placeholder claim" in p for p in probs) and any("kind 'x'" in p for p in probs)


def test_scout_briefs_one_per_kind_asked_for_else_every_kind():
    draft = {"checks": [{"kind": "client", "claim": "redis-py passes", "weight": 4, "hint": "redis-py"},
                         {"kind": "client", "claim": "redis-cli PING", "weight": 2}, {"kind": "docs", "claim": "README lists commands"}]}
    briefs = {b["kind"]: b for b in a.scout_briefs(draft)}
    assert set(briefs) == {"client", "docs"} and briefs["client"]["weight"] == 4
    assert briefs["client"]["claims"] == ["redis-py passes", "redis-cli PING"] and briefs["client"]["hints"] == ["redis-py"]
    assert [b["kind"] for b in a.scout_briefs({})] == list(a.SCOUT_KINDS)
    assert a.scout_briefs(draft, external=False) == []


def test_intake_route_rejects_a_plan_whose_contract_draft_has_a_placeholder_claim(fake_task_input):
    plan = {"kind": "feature", "confidence": 0.9, "subtasks": [{"id": "a", "description": "build the server",
                                                               "files": ["a.go"], "acceptanceCriteria": ["serves PING"]}],
            "contract": {"checks": [{"kind": "client", "claim": "ok"}]}}
    out = intake_route(fake_task_input(classification=plan, forceTeam=True, requireCode=True)).output_data
    assert out["route"] == "replan" and any("placeholder claim" in r for r in out["replanReasons"])
    plan["contract"]["checks"][0]["claim"] = "redis-py's string tests pass against port 2345"
    out = intake_route(fake_task_input(classification=plan, forceTeam=True, requireCode=True)).output_data
    assert out["route"] == "team" and out["contractDraft"]["checks"][0]["kind"] == "client"


# ─────────────────────────── the contract: red-proof, score ───────────────────
def test_fold_contract_drops_vacuous_keeps_blocked_and_records_absent_kinds():
    scouts = [{"kind": "client", "found": True, "claim": "redis-py passes", "command": "python -m pytest t.py", "weight": 4},
              {"kind": "scenario", "found": True, "claim": "smoke flow", "command": "sh .cc/checks/smoke.sh"},
              {"kind": "budget", "found": True, "claim": "p99 under 5ms", "command": "python bench.py", "weight": 1},
              {"kind": "docs", "found": False, "reason": "the request asks for no documentation"}]
    runs = {"python -m pytest t.py": {"exitCode": 1, "output": "FAILED"},
            "sh .cc/checks/smoke.sh": {"exitCode": 0, "output": "ok"},
            "python bench.py": {"error": "setup 'pip install x' exited 1"}}
    c = a.fold_contract({"threshold": 1.0}, scouts, runs)
    ids = {o["id"]: o for o in c["checks"]}
    assert set(ids) == {"acceptance", "integration", "client-redis-py-passes", "budget-p99-under-5ms"}
    assert ids["client-redis-py-passes"]["state"] == "red" and ids["client-redis-py-passes"]["weight"] == 4
    assert ids["budget-p99-under-5ms"]["state"] == "blocked" and "setup" in ids["budget-p99-under-5ms"]["mechanism"]
    assert c["dropped"][0]["id"] == "scenario-smoke-flow" and "vacuous" in c["dropped"][0]["mechanism"]
    assert c["absentKinds"] == {"docs": "the request asks for no documentation"}
    assert c["revision"] == 1 and c["threshold"] == 1.0
    # Every program carries the swarm's own two checks, red before any work.
    assert ids["acceptance"]["source"] == "swarm" and ids["integration"]["weight"] == 3
    assert a.contract_paths(c) == [a.CONTRACT_FILE]


def test_score_is_the_weighted_share_passing_and_blocked_is_never_a_pass():
    c = {"revision": 1, "threshold": 1.0, "checks": [
        {"id": "x", "weight": 3}, {"id": "y", "weight": 1}, {"id": "z", "weight": 1}]}
    assert a.score(c, _results(x="green", y="red", z="red")) == 0.6
    assert a.score(c, _results(x="green", y="blocked", z="green")) == 0.8
    assert a.score(c, {}) == 0.0
    # Reproducible: the same results score the same; a satisfied check counts as passing without a run.
    assert a.score(c, _results(x="green", y="red", z="red")) == a.score(c, _results(x="green", y="red", z="red"))
    c["checks"][1]["state"] = "satisfied"
    assert a.score(c, _results(x="green", z="red")) == 0.8
    assert [o["id"] for o in a.red_checks(c, _results(x="green", z="red"))] == ["z"]
    assert a.at_threshold({"threshold": 0.8}, 0.8) and not a.at_threshold({"threshold": 1.0}, 0.99)


def test_check_argv_refuses_shell_operators():
    assert a.check_argv("python -m pytest tests/x.py -k 'set or get'") == ["python", "-m", "pytest", "tests/x.py", "-k", "set or get"]
    for bad in ("a | b", "a && b", "a > out", ""):
        try:
            a.check_argv(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


# ─────────────────────────── amendments ───────────────────────────────────────
def test_amendments_make_the_next_revision_only_on_approve():
    c = _contract()
    c["checks"].append({"id": "client-x", "kind": "client", "claim": "x", "weight": 2, "state": "red", "command": "x", "setup": []})
    same = a.apply_amendment(c, {"op": "drop", "check": "client-x", "reason": "out of scope"}, "reject")
    assert same["revision"] == 1 and len(same["checks"]) == 3
    nxt = a.apply_amendment(c, {"op": "drop", "check": "client-x", "reason": "out of scope"}, "approve", by="architect")
    assert nxt["revision"] == 2 and [o["id"] for o in nxt["checks"]] == ["acceptance", "integration"]
    assert nxt["history"][-1]["op"] == "drop" and nxt["history"][-1]["by"] == "architect"
    # The swarm's own checks cannot be dropped; a satisfy counts the check as passing.
    kept = a.apply_amendment(c, {"op": "drop", "check": "acceptance", "reason": "x"}, "approve")
    assert kept["revision"] == 1
    sat = a.apply_amendment(c, {"op": "satisfy", "check": "client-x", "reason": "owner accepted"}, "approve", by="human:v")
    assert sat["revision"] == 2 and a.score(sat, {}) == 0.25
    th = a.apply_amendment(c, {"op": "threshold", "threshold": 0.8, "reason": "docs advisory"}, "approve")
    assert th["threshold"] == 0.8 and th["revision"] == 2
    w = a.apply_amendment(c, {"op": "weights", "weights": {"client-x": 0, "integration": 5}, "reason": "review"}, "approve")
    assert [(o["id"], o["weight"]) for o in w["checks"]] == [("acceptance", 3), ("integration", 5)]
    assert a.amendment_problems({"op": "drop", "reason": "x"}) and not a.amendment_problems({"op": "drop", "check": "y", "reason": "because"})


# ─────────────────────────── the ladder ───────────────────────────────────────
def test_ladder_progress_keeps_the_rung_and_stalls_advance_it():
    h = []
    assert a.next_rung(h, 1.0, 1.0, 2) == a.DELIVER
    assert a.next_rung(h, 0.6, 1.0, 2) == "rework"
    h = [{"rung": "rework", "score": 0.6}]
    assert a.next_rung(h, 0.8, 1.0, 2) == "rework"            # progress keeps the rung
    h.append({"rung": "rework", "score": 0.8})
    assert a.next_rung(h, 0.8, 1.0, 2) == "rework"            # first round without progress
    h.append({"rung": "rework", "score": 0.8})
    assert a.next_rung(h, 0.8, 1.0, 2) == "change_approach"   # two rounds without progress advance
    h.append({"rung": "change_approach", "score": 0.8})
    assert a.next_rung(h, 0.9, 1.0, 2) == "change_approach"
    h += [{"rung": "change_approach", "score": 0.9}, {"rung": "change_approach", "score": 0.9}]
    assert a.next_rung(h, 0.9, 1.0, 2) == "second_opinion"
    assert a.next_rung(h, 0.9, 1.0, 2, opinion_used=True) == a.NEEDS
    h.append({"rung": "second_opinion", "score": 0.9})
    assert a.next_rung(h, 0.9, 1.0, 2) == a.NEEDS
    # An override, a pending amendment, a spent budget and a forced rung come first, in that order.
    assert a.next_rung(h, 0.9, 1.0, 2, overridden=True) == a.DELIVER
    assert a.next_rung(h, 0.9, 1.0, 2, pending_amendment=True) == a.AMEND
    assert a.next_rung([], 0.5, 1.0, 2, budget_spent=True) == a.NEEDS
    assert a.next_rung([], 0.5, 1.0, 2, needs=[{"kind": "credential"}]) == a.NEEDS
    assert a.next_rung(h, 0.9, 1.0, 2, forced="change_approach") == "change_approach"
    assert a.tier_for([{"rung": "rework"}, {"rung": "rework"}], "rework") == 2 and a.tier_for([], "rework") == 0


def test_same_approach_and_fold_findings_apply_the_placeholder_rule():
    prev = [{"id": "fix-a", "files": ["a.go"], "acceptanceCriteria": ["PING returns PONG"]}]
    assert a.same_approach(prev, [{"id": "fix-a", "files": ["a.go"], "acceptanceCriteria": ["x"]}])
    assert a.same_approach(prev, [{"id": "other", "files": ["a.go"], "acceptanceCriteria": ["PING returns PONG"]}])
    assert not a.same_approach(prev, [{"id": "other", "files": ["b.go"], "acceptanceCriteria": ["PING returns PONG"]}])
    subs = [{"id": "resp", "files": ["resp/reader.go"]}, {"id": "store", "items": [{"id": "s1", "files": ["store/zset.go"]}]}]
    kept, rejected = a.fold_findings([
        {"id": "g1", "subsystem": "nope", "description": "dispatcher never calls the zset API", "files": ["store/zset.go", "cmd/dispatch.go"],
         "acceptanceCriteria": ["ZADD then ZRANGE through the dispatcher returns the member"]},
        {"id": "g2", "description": "test", "files": ["x"], "acceptanceCriteria": ["y"]},
        {"id": "g3", "description": "real", "files": [], "acceptanceCriteria": ["y"]},
        {"id": "g4", "description": "real", "files": ["x"], "acceptanceCriteria": ["ok"]},
        {"id": "g1", "description": "dup", "files": ["x"], "acceptanceCriteria": ["y"]}], subsystems=subs)
    assert [k["id"] for k in kept] == ["g1"] and kept[0]["subsystem"] == "store"
    assert len(rejected) == 4 and "placeholder" in rejected[0] and "no files" in rejected[1] and "criteria" in rejected[2]


# ─────────────────────────── steer, needs, report ─────────────────────────────
def test_steer_is_folded_once_and_each_shape_is_validated():
    facts = [{"key": "1", "author": "human:v", "data": {"shape": "feedback", "text": "the zset API is wrong"}},
             {"key": "2", "author": "human:v", "data": {"shape": "accept", "check": "docs-readme", "text": "good enough"}},
             {"key": "3", "author": "human:v", "data": {"shape": "amend", "amendment": "arch-1-1", "decision": "approve"}},
             {"key": "4", "author": "human:v", "data": {"shape": "budget", "ceilingUsd": 90}},
             {"key": "5", "author": "human:v", "data": {"shape": "budget", "ceilingUsd": 60}}]
    out = a.fold_steer(facts, {"1"})
    assert out["feedback"] == [] and out["accepts"][0]["check"] == "docs-readme" and out["accepts"][0]["op"] == "satisfy"
    assert out["decisions"] == {"arch-1-1": {"decision": "approve", "by": "human:v"}} and out["ceilingUsd"] == 90
    assert out["seen"] == ["2", "3", "4", "5"]
    assert a.steer_problems("feedback", {}) and not a.steer_problems("feedback", {"text": "x"})
    assert a.steer_problems("amend", {"amendment": "a"}) and not a.steer_problems("amend", {"amendment": "a", "decision": "reject"})
    assert a.steer_problems("budget", {"ceilingUsd": 0}) and a.steer_problems("nudge", {})


def test_needs_and_the_report_never_grade_a_failure():
    c = _contract()
    c["checks"].append({"id": "client-x", "kind": "client", "claim": "x", "weight": 2, "state": "blocked", "need": "a licence key",
                         "command": "x", "setup": []})
    needs = a.needs_of(c, {}, budget_spent=True, spent=12.5, ceiling=10)
    assert [n["kind"] for n in needs] == ["budget", "credential"] and "$12.50 of $10.00" in needs[0]["detail"]
    rep = a.program_report(delivered=False, contract=c, results={}, s=0.0, evaluations=[{"rank": "final", "score": 0.0, "rung": "needs", "revision": 1}],
                           needs=needs, override=None, done_count=1, total=2, final_head="abc123", ranks=[["m"], ["s"]])
    assert rep["verdict"] == "PROGRAM_NEEDS" and rep["programDelivered"] is False and "budget" in rep["report"]
    assert "INCOMPLETE" not in rep["report"] and rep["needs"] == needs and len(rep["checks"]) == 3
    ok_ = a.program_report(delivered=True, contract=c, results=_results(acceptance="green", integration="green"), s=1.0,
                           evaluations=[], needs=[], override={"reasoning": "the red check is out of scope"}, done_count=2,
                           total=2, final_head="abc123", ranks=[["m"], ["s"]])
    assert ok_["verdict"] == "PROGRAM_DELIVERED" and "override" in ok_["report"]
    assert "redis" not in a.contract_text(c) and "client-x" in a.contract_text(c, {})


# ─────────────────────────── the tasks: checks run, facts land ───────────────
def _repo(tmp_path):
    repo = str(tmp_path / "repo")
    git.ensure_ready(repo)
    with open(os.path.join(repo, "app.py"), "w") as fh:
        fh.write("print('hi')\n")
    git.git(repo, "add", "-A")
    git.git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "base")
    return repo


def _script(repo, rel, body):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def test_run_check_records_every_run_blocks_tree_edits_and_failed_setup(tmp_path):
    repo = _repo(tmp_path)
    _script(repo, ".cc/checks/red.sh", "#!/bin/sh\necho 'FAIL: PING' >&2\nexit 3\n")
    _script(repo, ".cc/checks/edit.sh", "#!/bin/sh\necho x >> app.py\nexit 0\n")
    r = run_check(repo, {"command": "sh .cc/checks/red.sh", "setup": []})
    assert r["state"] == "red" and r["exitCode"] == 3 and "FAIL: PING" in r["tail"] and r["seconds"] >= 0
    r = run_check(repo, {"command": "sh .cc/checks/edit.sh", "setup": []})
    assert r["state"] == "blocked" and "edited the repository tree" in r["mechanism"]
    assert open(os.path.join(repo, "app.py")).read() == "print('hi')\n"
    r = run_check(repo, {"command": "sh .cc/checks/red.sh", "setup": ["sh -c 'exit 9'"]})
    assert r["state"] == "blocked" and "setup" in r["mechanism"] and "exited 9" in r["mechanism"]
    r = run_check(repo, {"command": "a | b", "setup": []})
    assert r["state"] == "blocked" and "shell operator" in r["mechanism"]
    r = run_check(repo, {"command": "true", "setup": []})
    assert r["state"] == "green"


def test_definition_phase_red_proves_scouts_commits_the_contract_and_bounds_review(monkeypatch, tmp_path, fake_task_input):
    _iso(monkeypatch, tmp_path)
    repo = _repo(tmp_path)
    cid = "prog-arch"
    program_rank_init(fake_task_input(correlationId=cid, plan={"subtasks": [{"id": "srv", "files": ["app.py"]}], "contractPaths": []}))
    draft = {"threshold": 1.0, "checks": [{"kind": "client", "claim": "redis-cli PING answers PONG", "weight": 4},
                                           {"kind": "docs", "claim": "README documents PING"}]}
    sb = check_scout_build(fake_task_input(correlationId=cid, workspacePath=repo, lineage="", draft=draft,
                                            description="a server", externalChecks=True, maxRepairRounds=2)).output_data
    assert sb["define"] == "yes" and sb["scouts"] == "yes" and sb["kinds"] == ["client", "docs"]
    scout = sb["dynamicTasksInput"]["scout_client"]
    assert scout["allowedWriteRoots"] == [a.CHECK_ROOT, a.CACHE_ROOT] and scout["templateKey"] == "check_scout"
    assert "Bash" in scout["allowedTools"] and scout["maxTurns"] == 50  # redis-cli, pip, mkdir: every command; bounded effort
    assert "redis-cli PING answers PONG" in scout["prompt"] and os.path.isdir(scout["worktreePath"])
    assert sb["maxConvergenceIterations"] == 12
    # The scouts' scripts: one red, one vacuous (passes before any work), one not found.
    _script(scout["worktreePath"], ".cc/checks/ping.sh", "#!/bin/sh\nexit 1\n")
    _script(scout["worktreePath"], ".cc-cache/venv/bin/redis-cli", "#!/bin/sh\necho PONG\n")  # the scout's tool cache
    docs_wt = sb["dynamicTasksInput"]["scout_docs"]["worktreePath"]
    _script(docs_wt, ".cc/checks/readme.sh", "#!/bin/sh\nexit 0\n")
    scouts = {"scout_client__1": {"output": {"structured": {"found": True, "kind": "client", "claim": "redis-cli PING answers PONG",
                                                            "command": "sh .cc/checks/ping.sh", "setup": [], "weight": 4},
                                             "filesChanged": [".cc/checks/ping.sh"], "costUsd": 0.2}},
              "scout_docs__1": {"output": {"structured": {"found": True, "kind": "docs", "claim": "README documents PING",
                                                          "command": "sh .cc/checks/readme.sh"}, "filesChanged": [".cc/checks/readme.sh"]}},
              "scout_scenario__1": {"output": {"structured": {"found": False, "kind": "scenario", "reason": "nothing to drive yet"}}}}
    cf = contract_fold(fake_task_input(correlationId=cid, workspacePath=repo, scouts=scouts, draft=draft, revised={},
                                       externalChecks=True)).output_data
    c = cf["contract"]
    ids = {o["id"]: o for o in c["checks"]}
    assert set(ids) == {"acceptance", "integration", "client-redis-cli-ping-answers-pong"}
    assert ids["client-redis-cli-ping-answers-pong"]["state"] == "red" and ids["client-redis-cli-ping-answers-pong"]["paths"] == [".cc/checks/ping.sh"]
    assert c["dropped"][0]["kind"] == "docs" and c["absentKinds"]["scenario"] == "nothing to drive yet"
    assert cf["commit"] and os.path.isfile(os.path.join(repo, ".cc/contract.json")) and os.path.isfile(os.path.join(repo, ".cc/checks/ping.sh"))
    assert git.git(repo, "status", "--porcelain").stdout.strip() == ""  # committed on the branch
    assert not os.path.isdir(scout["worktreePath"])  # scout worktrees are gone
    # The scout's tool cache was seeded into the shared worktree (and from there into every fork and
    # snapshot), so the red-proof and the teams never download it again; it stays git-excluded.
    assert os.path.isfile(os.path.join(repo, ".cc-cache/venv/bin/redis-cli"))
    fork = git.worktree_add(repo, "impl")["worktreePath"]
    assert os.path.isfile(os.path.join(fork, ".cc-cache/venv/bin/redis-cli"))
    assert git.git(repo, "status", "--porcelain").stdout.strip() == "" and git.git(fork, "status", "--porcelain").stdout.strip() == ""
    git.worktree_remove(repo, "impl")
    # Frozen for every team: the program's contract paths now hold the contract file and the scripts.
    prog = bb.read_facts(cid, kind="program")[-1]["data"]
    assert prog["contractPaths"] == [".cc/contract.json", ".cc/checks/ping.sh"]
    from common import budget_gate
    assert budget_gate.read_spend(cid)["usd"] == 0.2
    # Review, one round: a reject applies the reviewer's weights as revision 2 and the build starts.
    cf2 = contract_fold(fake_task_input(correlationId=cid, workspacePath=repo, scouts={}, draft=draft)).output_data
    assert cf2["exists"] is True and cf2["revision"] == 1  # a second fold on the same board changes nothing
    rf = contract_review_fold(fake_task_input(correlationId=cid, workspacePath=repo, mode="contract", round=1, maxRounds=1,
                                              review={"structured": {"verdict": "reject", "findings": ["weight too high"],
                                                                     "weights": {"client-redis-cli-ping-answers-pong": 1}}})).output_data
    assert rf["approved"] == "yes" and rf["bound"] is True and rf["revision"] == 2
    assert [o for o in contract_of(cid)["checks"] if o["id"].startswith("client")][0]["weight"] == 1
    assert "review bound reached" in contract_of(cid)["mechanism"]
    assert json.load(open(os.path.join(repo, ".cc/contract.json")))["revision"] == 2  # the branch carries revision 2
    # A second definition on the same board is a no-op; a continuation copies the lineage's state.
    assert check_scout_build(fake_task_input(correlationId=cid, workspacePath=repo, lineage="", draft=draft)).output_data["define"] == "no"
    cont = check_scout_build(fake_task_input(correlationId="prog-cont", workspacePath=repo, lineage=f"continue:{cid}", draft={})).output_data
    assert cont["define"] == "no" and cont["copiedFrom"] == cid and contract_of("prog-cont")["revision"] == 2
    assert contract_of("prog-cont")["copiedFrom"] == cid


def test_evaluation_scores_the_head_files_items_and_the_ladder_ends_only_delivered_or_needs(monkeypatch, tmp_path, fake_task_input):
    _iso(monkeypatch, tmp_path)
    repo = _repo(tmp_path)
    cid = "prog-ladder"
    subs = [{"id": "srv", "files": ["app.py"], "description": "the server"}, {"id": "cli", "files": ["cli.py"], "dependsOn": ["srv"]}]
    program_rank_init(fake_task_input(correlationId=cid, plan={"subtasks": subs, "contractPaths": []}))
    _script(repo, ".cc/checks/ping.sh", "#!/bin/sh\ntest -f PONG\n")
    git.git(repo, "add", "-A")
    git.git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "check")
    c = a.fold_contract({"threshold": 1.0}, [{"kind": "client", "found": True, "claim": "PING answers PONG",
                                             "command": "sh .cc/checks/ping.sh", "weight": 2}],
                        {"sh .cc/checks/ping.sh": {"exitCode": 1}}, external=False)
    bb.post_fact(cid, "architect", a.CONTRACT_KIND, c)
    # Rank evaluation: everything is red (no acceptance test, no integration run, PONG missing).
    assert contract_run(fake_task_input(correlationId=cid, workspacePath=repo, stage="rank", rank=[], description="d",
                                        ceilingUsd=10, maxRepairRounds=2, testsPassed="")).output_data["evaluate"] == "no"
    run = contract_run(fake_task_input(correlationId=cid, workspacePath=repo, stage="rank", rank=["srv"], description="d",
                                       ceilingUsd=10, maxRepairRounds=2, testsPassed="")).output_data
    assert run["score"] == 0.0 and run["rung"] == "" and run["results"]["acceptance"]["state"] == "blocked"
    assert "PING answers PONG" in run["evaluatePrompt"] and run["tier"] == 0 and run["evaluate"] == "yes"
    # The checks ran, and the Architect reads, on a detached snapshot of the head, not the shared worktree.
    assert run["worktree"] != repo and os.path.isdir(run["worktree"]) and git.head(run["worktree"]) == git.head(repo)
    assert os.path.basename(run["worktree"]).startswith("eval-")
    fold = architect_fold(fake_task_input(correlationId=cid, workspacePath=repo, stage="rank", rank=["srv"], run=run, ceilingUsd=10,
                                          maxRepairRounds=2, evaluation={"structured": {
                                              "assessment": "cli never calls the server",
                                              "items": [{"id": "wire-cli", "subsystem": "cli", "description": "call the server from the cli",
                                                         "files": ["cli.py"], "acceptanceCriteria": ["cli ping prints PONG"]},
                                                        {"id": "bad", "description": "x", "files": [], "acceptanceCriteria": ["y"]}],
                                              "directives": [{"subsystem": "cli", "text": "use the resp client"}],
                                              "amendments": [{"op": "reweight", "check": "client-ping-answers-pong", "weight": 1, "reason": "advisory"}]},
                                              "filesChanged": ["app.py"], "costUsd": 0.1})).output_data
    assert fold["itemsFiled"] == ["wire-cli"] and fold["rejected"] and fold["reverted"] == ["app.py"] and fold["amendments"] == ["arch-1-1"]
    prog = bb.read_facts(cid, kind="program")[-1]["data"]
    assert [it["id"] for it in prog["subsystems"][1]["items"]] == ["cli", "wire-cli"]  # the next rank builds both
    assert any(d["target"] == "subsystem:cli" for d in tb.fold_board(cid)["directives"])
    assert evaluations_of(cid)[-1]["stage"] == "rank" and evaluations_of(cid)[-1]["score"] == 0.0
    # Final evaluation, under threshold: a pending amendment is decided first, then the ladder starts at rework.
    _red_acceptance(cid)
    for sid in ("srv", "cli"):
        tb.set_state(cid, "program", sid, tb.DONE)
    run = contract_run(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", description="d", ceilingUsd=10,
                                       maxRepairRounds=2, testsPassed=True)).output_data
    assert run["results"]["integration"]["state"] == "green" and run["rung"] == a.AMEND
    fold = architect_fold(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", run=run, ceilingUsd=10, maxRepairRounds=2,
                                          evaluation={"structured": {"assessment": "PONG missing", "items": []}})).output_data
    assert fold["act"] == "amend" and "arch-1-1" in fold["amendText"] and fold["continue"] is True
    rf = contract_review_fold(fake_task_input(correlationId=cid, workspacePath=repo, mode="amendments",
                                              review={"structured": {"verdict": "approve"}})).output_data
    assert rf["approved"] == "yes" and rf["decided"] == ["arch-1-1"] and contract_of(cid)["revision"] == 2
    # Rework: a repair team is forked with the red checks (no items named → one per red check), one tier up per round.
    run = contract_run(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", description="d", ceilingUsd=10,
                                       maxRepairRounds=2, testsPassed="")).output_data
    assert run["results"]["integration"]["state"] == "green"  # the suite's result at this head stands
    assert run["rung"] == "rework"
    fold = architect_fold(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", run=run, ceilingUsd=10, maxRepairRounds=2,
                                          repoPath=repo, evaluation={"structured": {"assessment": "PONG missing", "items": []}})).output_data
    assert fold["act"] == "repair" and fold["dynamicTasks"][0]["subWorkflowParam"]["name"] == "team_build"
    team_in = fold["dynamicTasksInput"]["team_repair-1"]
    assert team_in["parentCorrelationId"] == cid and team_in["baseModelTier"] == 0 and team_in["forceTeam"] is True
    assert [it["id"] for it in team_in["subtasks"]] == ["repair-acceptance", "repair-client-ping-answers-pong"]
    assert team_in["contractPaths"] == [] and tb.fold_board(cid)["items"]["repair-1"]["state"] == tb.IN_PROGRESS
    # The repair lands PONG; the next pass folds the team, scores higher, and keeps the rung.
    with open(os.path.join(repo, "PONG"), "w") as fh:
        fh.write("1")
    git.git(repo, "add", "-A")
    git.git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "pong")
    head = contract_run(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", mode="head", repairJoined={
        "team_repair-1__3": {"output": {"delivered": False, "verdict": "INCOMPLETE", "spendUsd": 2.0, "report": "acceptance blocked"}}})).output_data
    assert head["head"] == git.head(repo) and head["repaired"][0]["subsystem"] == "repair-1"
    assert tb.fold_board(cid)["items"]["repair-1"]["state"] == tb.REVIEW
    run = contract_run(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", description="d", ceilingUsd=10,
                                       maxRepairRounds=2, testsPassed=True)).output_data
    assert run["results"]["client-ping-answers-pong"]["state"] == "green" and run["score"] == 0.5714 and run["rung"] == "rework"
    assert run["tier"] == 1
    # Budget spent: the ladder ends the run with a named need, never a block; the outcome is PROGRAM_NEEDS.
    from common import budget_gate
    budget_gate.record_spend(cid, "child:x", usd=20.0)
    run = contract_run(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", description="d", ceilingUsd=10,
                                       maxRepairRounds=2, testsPassed=True)).output_data
    assert run["rung"] == a.NEEDS and run["budgetSpent"] is True
    fold = architect_fold(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", run=run, ceilingUsd=10, maxRepairRounds=2,
                                          evaluation={"structured": {"assessment": "acceptance still blocked", "items": []}})).output_data
    assert fold["act"] == "none" and fold["continue"] is False and fold["needs"][0]["kind"] == "budget"
    out = program_outcome(fake_task_input(correlationId=cid, subsystems=subs, testsPassed=True, finalHead=git.head(repo))).output_data
    assert out["verdict"] == "PROGRAM_NEEDS" and out["programDelivered"] is False and "$22.10 of $10.00" in out["report"]
    assert out["score"] == 0.5714 and [o["id"] for o in out["checks"] if not o["passed"]] == ["acceptance"]
    view = board_view(cid)
    assert view["rung"] == a.NEEDS and view["needs"][0]["kind"] == "budget" and len(view["history"]) == 4
    # Human steer: accept the red check; the next evaluation counts it and the program delivers.
    bb.post_fact(cid, "human:viren", a.STEER_KIND, {"shape": "accept", "check": "acceptance", "text": "tests are mine to add later"})
    bb.post_fact(cid, "human:viren", a.STEER_KIND, {"shape": "feedback", "text": "ship it"})
    run = contract_run(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", description="d", ceilingUsd=100,
                                       maxRepairRounds=2, testsPassed=True)).output_data
    assert "ship it" in run["evaluatePrompt"]
    fold = architect_fold(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", run=run, ceilingUsd=100, maxRepairRounds=2,
                                          evaluation={"structured": {"assessment": "done", "items": []}})).output_data
    assert fold["delivered"] is True and fold["score"] == 1.0 and contract_of(cid)["revision"] == 3
    assert fold["continue"] is False
    out = program_outcome(fake_task_input(correlationId=cid, subsystems=subs, testsPassed=True, finalHead=git.head(repo))).output_data
    assert out["verdict"] == "PROGRAM_DELIVERED" and "accepted" in out["report"]
    # Steer is folded once: a second evaluation sees nothing new.
    assert a.fold_steer(__import__("architect.tasks", fromlist=["steer_facts"]).steer_facts(cid),
                        __import__("architect.tasks", fromlist=["seen_steer"]).seen_steer(cid))["seen"] == []


def test_second_opinion_overrides_once_per_revision_or_sends_back(monkeypatch, tmp_path, fake_task_input):
    _iso(monkeypatch, tmp_path)
    repo = _repo(tmp_path)
    cid = "prog-opinion"
    program_rank_init(fake_task_input(correlationId=cid, plan={"subtasks": [{"id": "srv", "files": ["app.py"]}], "contractPaths": []}))
    tb.set_state(cid, "program", "srv", tb.DONE)
    _red_acceptance(cid)
    bb.post_fact(cid, "architect", a.CONTRACT_KIND, _contract())
    history = [{"rung": "rework", "score": 0.5}, {"rung": "rework", "score": 0.5}, {"rung": "rework", "score": 0.5},
               {"rung": "change_approach", "score": 0.5}, {"rung": "change_approach", "score": 0.5}, {"rung": "change_approach", "score": 0.5}]
    for h in history:
        bb.post_fact(cid, "architect", a.EVALUATION_KIND, {"stage": "final", "rank": "final", "revision": 1, **h, "items": []})
    run = contract_run(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", description="d", ceilingUsd=100,
                                       maxRepairRounds=2, testsPassed=True)).output_data
    assert run["rung"] == "second_opinion"
    fold = architect_fold(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", run=run, ceilingUsd=100, maxRepairRounds=2,
                                          evaluation={"structured": {"assessment": "acceptance blocked, nothing to run", "items": []}})).output_data
    assert fold["act"] == "second_opinion" and "acceptance blocked" in fold["opinionPrompt"]
    # Short: items for a change of approach; the next pass is forced there.
    op = opinion_fold(fake_task_input(correlationId=cid, workspacePath=repo, opinion={"structured": {
        "verdict": "short", "reasoning": "no test exists", "items": [{"id": "add-tests", "description": "write the acceptance test",
                                                                       "files": ["tests/test_app.py"], "acceptanceCriteria": ["pytest passes"]}]}})).output_data
    assert op["verdict"] == "short" and op["items"] == ["add-tests"]
    run = contract_run(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", description="d", ceilingUsd=100,
                                       maxRepairRounds=2, testsPassed=True)).output_data
    assert run["rung"] == "change_approach" and "add-tests" in run["evaluatePrompt"]
    # The same plan again is a replan (one tier up), not a repair.
    bb.post_fact(cid, "architect", a.EVALUATION_KIND, {"stage": "final", "rank": "final", "revision": 1, "rung": "change_approach", "score": 0.5,
                                                       "items": [{"id": "add-tests", "files": ["tests/test_app.py"], "acceptanceCriteria": ["pytest passes"]}]})
    fold = architect_fold(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", run=run, ceilingUsd=100, maxRepairRounds=2,
                                          evaluation={"structured": {"assessment": "x", "items": [
                                              {"id": "add-tests", "description": "same", "files": ["tests/test_app.py"],
                                               "acceptanceCriteria": ["pytest passes"]}]}})).output_data
    assert fold["rung"] == a.REPLAN and fold["act"] == "none" and fold["continue"] is True
    # Meets: an override delivers, recorded with the reasoning; a second opinion never runs twice for a revision.
    op = opinion_fold(fake_task_input(correlationId=cid, workspacePath=repo, opinion={"structured": {
        "verdict": "meets", "reasoning": "the request never asked for tests"}})).output_data
    assert op["verdict"] == "meets"
    run = contract_run(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", description="d", ceilingUsd=100,
                                       maxRepairRounds=2, testsPassed=True)).output_data
    assert run["rung"] == a.DELIVER and run["evaluate"] == "no" and "evaluatePrompt" not in run  # the sign-off is the score
    out = program_outcome(fake_task_input(correlationId=cid, subsystems=[{"id": "srv", "files": ["app.py"]}], testsPassed=True)).output_data
    assert out["verdict"] == "PROGRAM_NEEDS"  # no final fold recorded deliver yet
    fold = architect_fold(fake_task_input(correlationId=cid, workspacePath=repo, stage="final", run=run, ceilingUsd=100, maxRepairRounds=2,
                                          evaluation={})).output_data  # no session ran
    assert fold["delivered"] is True
    out = program_outcome(fake_task_input(correlationId=cid, subsystems=[{"id": "srv", "files": ["app.py"]}], testsPassed=True)).output_data
    assert out["verdict"] == "PROGRAM_DELIVERED" and "never asked for tests" in out["report"]
    # contract_wrong without a recordable amendment is judged short.
    op = opinion_fold(fake_task_input(correlationId=cid, workspacePath=repo, opinion={"structured": {
        "verdict": "contract_wrong", "reasoning": "x", "amendments": [{"op": "drop", "reason": "x"}]}})).output_data
    assert op["verdict"] == "short" and op["rejected"]


def test_the_definition_of_done_on_the_branch_reaches_every_builder_prompt(monkeypatch, tmp_path, fake_task_input):
    """One source of done: the scout's check script (the binding interface) is read from the branch
    into the test author's, the implementer's and the reviewer's prompts; the scouts read the plan."""
    from team.tasks import team_acceptance_build, team_plan_init, team_review_build, team_standup
    _iso(monkeypatch, tmp_path)
    repo = _repo(tmp_path)
    cid = "prog-wall"
    subs = [{"id": "calc", "files": ["calc.go"], "items": [{"id": "ops", "description": "the four operations",
                                                           "files": ["calc.go"], "acceptanceCriteria": ["`calc add 3 4` prints 7"]}]}]
    program_rank_init(fake_task_input(correlationId=cid, plan={"subtasks": subs, "contractPaths": []}))
    sb = check_scout_build(fake_task_input(correlationId=cid, workspacePath=repo, lineage="", draft={
        "checks": [{"kind": "scenario", "claim": "basic arithmetic from the command line"}]})).output_data
    scout = sb["dynamicTasksInput"]["scout_scenario"]
    assert "calc/ops" in scout["prompt"] and "`calc add 3 4` prints 7" in scout["prompt"] and "BINDING INTERFACE" in scout["prompt"]
    _script(scout["worktreePath"], ".cc/checks/scenario.sh", "#!/bin/sh\ntest \"$(./calc add 3 4)\" = 7\n")
    scouts = {"scout_scenario__1": {"output": {"structured": {
        "found": True, "kind": "scenario", "claim": "basic arithmetic from the command line",
        "command": "sh .cc/checks/scenario.sh", "interface": "`./calc add 3 4` prints `7`", "weight": 2},
        "filesChanged": [".cc/checks/scenario.sh"]}}}
    cf = contract_fold(fake_task_input(correlationId=cid, workspacePath=repo, scouts=scouts, draft={}, revised={},
                                       externalChecks=True)).output_data
    assert cf["checks"] == 3 and "interface: `./calc add 3 4` prints `7`" in cf["contractText"]
    brief = a.read_contract_brief(repo)
    assert "DEFINITION OF DONE" in brief and 'test "$(./calc add 3 4)" = 7' in brief and "the check wins" in brief
    assert "- acceptance [acceptance" in brief and a.read_contract_brief(str(tmp_path / "nowhere")) == ""
    assert a.contract_brief(a.fold_contract({}, [], {}, external=False)) == ""  # own checks only: nothing to add
    # The three team roles, on a child team of this program, all read the same text from the branch.
    tid = "team-calc"
    team_plan_init(fake_task_input(correlationId=tid, subtasks=subs[0]["items"], contractPaths=[a.CONTRACT_FILE]))
    author = team_acceptance_build(fake_task_input(correlationId=tid, repoPath=repo)).output_data
    assert 'test "$(./calc add 3 4)" = 7' in author["dynamicTasksInput"]["accept_ops"]["prompt"]
    assert "drive the program EXACTLY as these checks" in author["dynamicTasksInput"]["accept_ops"]["prompt"]
    stand = team_standup(fake_task_input(correlationId=tid, repoPath=repo, parentCorrelationId=cid, subsystemId="calc",
                                         authorAcceptanceTests=False)).output_data
    assert 'test "$(./calc add 3 4)" = 7' in stand["dynamicTasksInput"]["ops"]["prompt"]
    tb.set_state(tid, "lead", "ops", tb.REVIEW)
    review = team_review_build(fake_task_input(correlationId=tid, sharedRepo=repo, finalHead=git.head(repo))).output_data
    assert 'test "$(./calc add 3 4)" = 7' in review["dynamicTasksInput"]["review_ops"]["prompt"]
    assert "is a blocking finding: name the check" in review["dynamicTasksInput"]["review_ops"]["prompt"]


def test_apply_rank_hands_a_spent_or_exhausted_team_to_the_architect(monkeypatch, tmp_path, fake_task_input):
    _iso(monkeypatch, tmp_path)
    cid = "prog-hand"
    program_rank_init(fake_task_input(correlationId=cid, plan={"subtasks": [{"id": "m", "files": ["m.py"]}], "contractPaths": []},
                                      maxTeamAttempts=1))
    ap = program_apply_rank(fake_task_input(correlationId=cid, rank=["m"], maxTeamAttempts=1, joined={
        "team_m__1": {"output": {"delivered": False, "verdict": "INCOMPLETE", "report": "still red"}}})).output_data
    assert ap["continue"] is False
    item = tb.fold_board(cid)["items"]["m"]
    assert item["state"] == tb.REVIEW and "handed to the architect" in item["reason"]
    assert tb.BLOCKED not in {it["state"] for it in tb.fold_board(cid)["items"].values()}


def _walk(tasks):
    for t in tasks:
        yield t
        for k in ("loopOver", "defaultCase"):
            yield from _walk(t.get(k) or [])
        for b in (t.get("decisionCases") or {}).values():
            yield from _walk(b)
        for b in t.get("forkTasks") or []:
            yield from _walk(b)


def test_cc_program_workflow_holds_the_definition_phase_and_the_ladder():
    wf = json.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "cc_program.json")))
    refs = [t["taskReferenceName"] for t in wf["tasks"]]
    # The scouts define done beside the first rank's build (both read the plan's criteria); the fold
    # is the whole definition (no separate review session), and the final contract run is the sign-off.
    fork = next(t for t in wf["tasks"] if t["taskReferenceName"] == "define_and_build")
    assert [t["taskReferenceName"] for t in fork["forkTasks"][0]] == ["scout_build", "scout_gate", "define_gate"]
    assert [t["taskReferenceName"] for t in fork["forkTasks"][1]] == ["rank_loop_init", "rank_loop"]
    assert refs.index("rank_init") < refs.index("define_and_build") < refs.index("define_and_build_join") < refs.index("tests")
    # The planner reads the run's own checkout, never the repository root: the root holds .cc-worktrees
    # with earlier runs' checkouts (a stopped run's is kept for its retry), and a planner that globbed
    # it cited the previous run's frozen scenario as "pre-existing", so its implementer hunted the
    # whole filesystem for a file its own run had not written yet (run 74bd988c after b00146a9).
    assert refs.index("workspace") < refs.index("plan_source")
    plan = next(t for t in _walk(wf["tasks"]) if t["taskReferenceName"] == "program_plan")
    assert plan["inputParameters"]["worktreePath"] == "${workspace.output.worktreePath}"
    # The planner's structured-output schema is inlined in the workflow: when it lagged the draft
    # schema, the planner put the scenario script it was asked to write into `summary` and a scout
    # was forked anyway (run 62c7d8c7).
    assert plan["inputParameters"]["schema"]["properties"]["contract"] == a.DRAFT_SCHEMA
    rank_init = next(t for t in wf["tasks"] if t["taskReferenceName"] == "rank_init")
    assert rank_init["inputParameters"]["workspacePath"] == "${workspace.output.worktreePath}"
    gate = fork["forkTasks"][0][2]
    assert [t["taskReferenceName"] for t in gate["decisionCases"]["yes"]] == ["contract_fold"]
    assert "contract_review" not in [t["taskReferenceName"] for t in _walk(wf["tasks"])]
    rank_loop = fork["forkTasks"][1][1]["loopOver"]
    step = rank_loop[0]
    assert step["type"] == "FORK_JOIN" and [t["taskReferenceName"] for t in step["forkTasks"][0]] == ["rank_contract_run", "rank_eval_gate"]
    assert step["forkTasks"][0][0]["inputParameters"]["rank"] == "${workflow.variables.lastRank}"
    ev = step["forkTasks"][0][1]["decisionCases"]["yes"][0]
    assert ev["inputParameters"]["worktreePath"] == "${rank_contract_run.output.worktree}"  # a snapshot, never the shared worktree
    for ref in ("second_opinion", "amendment_review", "architect_evaluate"):
        task = next(t for t in _walk(wf["tasks"]) if t["taskReferenceName"] == ref)
        assert task["inputParameters"]["worktreePath"].endswith(".output.worktree}"), ref
    assert refs.index("tests") < refs.index("convergence_loop") < refs.index("outcome")
    assert wf["inputTemplate"]["externalChecks"] is True and wf["inputTemplate"]["maxRepairRounds"] == 2
    text = json.dumps(wf)
    assert '"WAIT"' not in text and "humanAvailable" not in text and "JSON_JQ_TRANSFORM" not in text
    loop = next(t for t in wf["tasks"] if t["taskReferenceName"] == "convergence_loop")
    body = [t["taskReferenceName"] for t in loop["loopOver"]]
    assert body == ["final_contract_run", "evaluate_gate", "architect_fold", "ladder", "capture_convergence"]
    # At the threshold the sign-off is the score: the Architect session runs only when contract_run asks.
    gate = loop["loopOver"][1]
    assert gate["inputParameters"] == {"evaluate": "${final_contract_run.output.evaluate}"}
    assert [t["taskReferenceName"] for t in gate["decisionCases"]["yes"]] == ["architect_evaluate"] and gate["defaultCase"] == []
    ladder = loop["loopOver"][3]
    assert set(ladder["decisionCases"]) == {"repair", "second_opinion", "amend"}
    repair = [t["taskReferenceName"] for t in ladder["decisionCases"]["repair"]]
    assert repair == ["repair_fan_out", "repair_join", "repair_head", "repair_tests"]
    tests = ladder["decisionCases"]["repair"][3]["inputParameters"]
    assert tests["candidateCommit"] == "${repair_head.output.head}"
    assert "continue" in loop["loopCondition"] and "${scout_build.output.maxConvergenceIterations}" in json.dumps(loop["inputParameters"])
    plan = next(t for t in next(t for t in wf["tasks"] if t["taskReferenceName"] == "plan_source")["decisionCases"]["plan"][0]["loopOver"]
                if t["taskReferenceName"] == "program_plan")
    assert "contract" in plan["inputParameters"]["schema"]["properties"]
    assert wf["outputParameters"]["needs"] == "${outcome.output.needs}" and "PROGRAM_INCOMPLETE" not in text


def test_architect_directive_named_for_one_item_reaches_only_that_item(monkeypatch, tmp_path, fake_task_input):
    """Rank two's Architect posted advice about sorted-set scores to the whole command-engine team; five
    implementers whose items had nothing to do with it pushed back, each costing a round (run 351812b2).
    A directive that names its item is handed to that implementer alone."""
    from team.tasks import _parent_directives
    _iso(monkeypatch, tmp_path)
    repo = _repo(tmp_path)
    cid = "prog-directive"
    program_rank_init(fake_task_input(correlationId=cid, plan={"subtasks": [
        {"id": "ce", "files": ["cmd/a.go"], "items": [{"id": "ce-zsets", "files": ["cmd/a.go"]}, {"id": "ce-keys", "files": ["cmd/k.go"]}]}],
        "contractPaths": []}))
    bb.post_fact(cid, "architect", a.CONTRACT_KIND, _contract())
    run = {"stage": "rank", "head": git.head(repo), "worktree": repo}
    architect_fold(fake_task_input(correlationId=cid, workspacePath=repo, stage="rank", rank=["x"], run=run, ceilingUsd=10,
                                   maxRepairRounds=2, evaluation={"structured": {"assessment": "ok", "items": [], "directives": [
                                       {"subsystem": "ce", "item": "ce-zsets", "text": "accept +inf scores in ZADD"},
                                       {"subsystem": "ce", "text": "every command answers RESP2 errors with -ERR"}]}}))
    seen = _parent_directives({"parentCorrelationId": cid, "subsystemId": "ce"})
    assert {(d["target"], d["text"][:12]) for d in seen} == {("ce-zsets", "accept +inf "), ("team", "every comman")}


def test_a_check_that_backgrounds_the_server_is_judged_by_its_run_not_by_reading_it():
    """A static rule dropped every shell check that backgrounds a command and kills it: the Postgres
    scenario (`"$BIN" --port 2345 &` ... `trap kill`) was dropped unrun, the run delivered at score 1.0
    without psql ever connecting, and the retrospective taught later runs not to trust the check
    (run 1cabdcac). The red-proof is the run: red against the untouched head enters the contract."""
    assert not hasattr(a, "lint_check_script")
    scouts = [{"kind": "scenario", "found": True, "claim": "psql works", "command": "bash .cc/checks/scenario.sh"}]
    red = {"exitCode": 1, "output": "FAIL: cargo build --release failed", "error": ""}
    assert a.red_proof(scouts[0], red) == {"state": "red", "mechanism": ""}
    c = a.fold_contract({"threshold": 1.0}, scouts, {"bash .cc/checks/scenario.sh": red})
    assert [o["kind"] for o in c["checks"]] == ["acceptance", "integration", "scenario"]
    assert c["dropped"] == []


def test_an_own_check_cannot_be_dropped_and_the_same_amendment_is_not_proposed_twice():
    """The Architect proposed dropping the red `integration` check five times; the reviewer approved
    each, each was a no-op (own checks stay), and the ladder looped for 24 minutes (run 3c84dd5d)."""
    contract = {"revision": 2, "checks": [
        {"id": "integration", "kind": "integration", "state": "red", "mechanism": "go 1.21 required, resolved go1.26", "weight": 3},
        {"id": "scenario-x", "kind": "scenario", "state": "green", "mechanism": "", "weight": 2}]}
    drop = {"op": "drop", "check": "integration", "reason": "environmental, duplicated by acceptance"}
    why = a.amendment_problems(drop, contract, [])
    assert "cannot be dropped" in why and "go 1.21 required" in why
    assert "cannot be dropped" in a.amendment_problems({**drop, "op": "reweight", "weight": 0}, contract, [])
    assert a.amendment_problems({**drop, "op": "reweight", "weight": 1}, contract, []) == ""
    ok = {"op": "drop", "check": "scenario-x", "reason": "the script backgrounds the binary"}
    assert a.amendment_problems(ok, contract, []) == ""
    decided = [{"id": "arch-3-1", **ok, "by": "architect", "revision": 2}, {"id": "arch-3-1", "decision": "approve"}]
    assert "already proposed for revision 2 (approve)" in a.amendment_problems(ok, contract, decided)
    assert a.amendment_problems(ok, {**contract, "revision": 3}, decided) == ""  # a new revision may revisit it
    # A reviewer's weights zeroed acceptance and integration (run a8167484): own checks keep their weight.
    applied = a.apply_amendment(contract, {"op": "weights", "weights": {"integration": 0, "scenario-x": 5}, "reason": "r"}, "approve")
    assert {o["id"]: o["weight"] for o in applied["checks"]} == {"integration": 3, "scenario-x": 5} and applied["revision"] == 3
    assert a.apply_amendment(contract, {"op": "weights", "weights": {"integration": 0}, "reason": "r"}, "approve") == contract


def test_a_check_runs_after_the_checkouts_dependency_installs(monkeypatch, tmp_path):
    """The scenario's `npm run dev` died with "vite: command not found" at the final head: a fresh
    snapshot has no node_modules. A check runs the checkout's declared installs first; one that fails
    blocks the check with its mechanism (run b00146a9)."""
    from common import verification
    repo = _repo(tmp_path)
    _script(repo, ".cc/checks/ok.sh", "#!/bin/sh\nexit 0\n")
    calls = []
    monkeypatch.setattr(verification, "_prepare_commands", lambda root: [{"argv": ["sh", "-c", "echo installed; exit 0"], "affectedUnit": "dependencies:."}])
    r = run_check(repo, {"command": "sh .cc/checks/ok.sh", "setup": []})
    assert r["state"] == "green"
    monkeypatch.setattr(verification, "_prepare_commands", lambda root: [{"argv": ["sh", "-c", "echo no registry >&2; exit 3"], "affectedUnit": "dependencies:."}])
    r = run_check(repo, {"command": "sh .cc/checks/ok.sh", "setup": []})
    assert r["state"] == "blocked" and "dependency install" in r["mechanism"] and "no registry" in r["mechanism"]


def test_every_join_is_synchronous_so_a_finished_branch_is_seen_at_once():
    """Conductor evaluates an asynchronous JOIN with exponential backoff once it has been polled
    more than systemTaskPostponeThreshold (200) times: a JOIN that waited on a 7-minute team build
    was re-evaluated every 30 s (the systemTaskWorkerCallbackDuration cap), so rank_step_join and
    define_and_build_join each noticed their finished branch 30 s late and the review joins 3 to 8 s
    late — 73 s of a 10-minute run spent in no task (run b00146a9). joinMode SYNC evaluates the JOIN
    on every system-task poll (50 ms) instead."""
    root = os.path.join(os.path.dirname(__file__), "..", "workflows")
    joins = {}
    for name in sorted(os.listdir(root)):
        if not name.endswith(".json"):
            continue
        wf = json.load(open(os.path.join(root, name)))
        for t in _walk(wf.get("tasks") or []):
            if t.get("type") == "JOIN":
                joins[f"{name}:{t['taskReferenceName']}"] = t.get("joinMode")
    assert joins and {"cc_program.json:rank_step_join", "cc_program.json:define_and_build_join",
                      "team_build.json:fan_join", "team_build.json:review_fan_join"} <= set(joins)
    assert [k for k, mode in joins.items() if mode != "SYNC"] == []
