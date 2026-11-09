"""Closure plan WP5 (H3): teams of teams — the program board, ranks of concurrent child teams,
program-scoped ownership, steering across levels, and serialized integration on one branch."""
import os
import threading

import common  # noqa: F401
from common import continuous_integrate, file_ownership, git, program, team_board as tb
from stages.tasks import program_apply_rank, program_outcome, program_rank_build, program_rank_init
from team.tasks import team_plan_init, team_standup
from common import blackboard as bb


def _iso(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))


SUBS = [
    {"id": "model", "files": ["model.py"]},
    {"id": "store", "files": ["store.py"], "dependsOn": ["model"],
     "items": [{"id": "s1", "files": ["store.py"]}, {"id": "s2", "files": ["store_idx.py"], "dependsOn": ["s1"]}]},
    {"id": "cli", "files": ["cli.py"], "dependsOn": ["model"]},
    {"id": "api", "files": ["api.py", "cli.py"], "dependsOn": ["model"]},
]


def test_rank_plan_splits_rank_mates_that_share_a_file():
    out = program.rank_plan(SUBS)
    assert out["ranks"] == [["model"], ["api", "store"], ["cli"]]  # cli and api share cli.py
    assert out["split"] == [{"rank": ["api", "cli", "store"], "into": [["api", "store"], ["cli"]]}]
    assert program.rank_plan(SUBS, contract_paths=["cli.py"])["ranks"] == [["model"], ["api", "cli", "store"]]
    assert [i["id"] for i in program.child_items(SUBS[1])] == ["s1", "s2"]
    assert program.child_items(SUBS[0])[0]["files"] == ["model.py"]


def test_program_board_ranks_children_and_rollups(monkeypatch, tmp_path, fake_task_input):
    _iso(monkeypatch, tmp_path)
    pid = "prog-1"
    init = program_rank_init(fake_task_input(correlationId=pid, plan={"subtasks": SUBS, "contractPaths": []})).output_data
    assert init["gate"] == "build" and init["rankCount"] == 3
    board = tb.fold_board(pid)
    assert set(board["items"]) == {"model", "store", "cli", "api"}
    assert board["items"]["store"]["files"] == ["store.py", "store_idx.py"]  # union of the team's items

    rb = program_rank_build(fake_task_input(correlationId=pid, repoPath="/r", workspacePath="/w",
                                            ceilingUsd=30, maxDepth=2)).output_data
    assert rb["rank"] == ["model"] and rb["childBudgetUsd"] == 30.0
    child = rb["dynamicTasksInput"]["team_model"]
    assert rb["dynamicTasks"][0]["subWorkflowParam"]["name"] == "team_build"
    assert child["parentCorrelationId"] == pid and child["workspacePath"] == "/w" and child["forceTeam"] is True

    # The child team registers itself on the program board (team tree).
    team_plan_init(fake_task_input(correlationId="child-model", subtasks=child["subtasks"], contractPaths=[],
                                   parentCorrelationId=pid, subsystemId="model"))
    assert bb.read_facts(pid, kind="child")[0]["data"] == {"subsystem": "model", "childRun": "child-model"}

    ap = program_apply_rank(fake_task_input(correlationId=pid, rank=["model"], joined={
        "team_model__1": {"output": {"delivered": True, "verdict": "DELIVERED", "spendUsd": 4.0,
                                     "rollup": {"total": 1, "done": 1}}}})).output_data
    assert ap["continue"] is True and ap["remainingRanks"] == 2
    from common import budget_gate
    assert budget_gate.by_role(budget_gate.read_spend(pid)["by_author"]) == {"child_team": 4.0}

    rb2 = program_rank_build(fake_task_input(correlationId=pid, ceilingUsd=30)).output_data
    assert rb2["rank"] == ["api", "store"] and rb2["childBudgetUsd"] == 13.0  # (30 - 4) / 2
    assert [i["id"] for i in rb2["dynamicTasksInput"]["team_store"]["subtasks"]] == ["s1", "s2"]
    ap2 = program_apply_rank(fake_task_input(correlationId=pid, rank=["api", "store"], maxTeamAttempts=1, joined={
        "team_api__2": {"output": {"delivered": True, "verdict": "DELIVERED"}},
        "team_store__2": {"output": {"delivered": False, "verdict": "INCOMPLETE", "report": "s2 blocked"}}})).output_data
    assert ap2["continue"] is False and ap2["rankDelivered"] is False
    assert "INCOMPLETE" in tb.fold_board(pid)["items"]["store"]["reason"]
    out = program_outcome(fake_task_input(correlationId=pid, subsystems=SUBS, testsPassed=True)).output_data
    assert out["programDelivered"] is False and out["verdict"] == "PROGRAM_INCOMPLETE"
    assert {c["subsystem"] for c in out["children"]} == {"model", "api", "store"}


def test_design_docs_reslice_the_remaining_subsystems_before_their_rank_is_built(monkeypatch, tmp_path,
                                                                                fake_task_input):
    """The planner slices every subsystem before the design team has written anything; twice the
    design then forbade what a pre-sliced item demanded (fc6467b7 st-lru, a062184f r-value) and the
    reviewers enforced the design against work the plan asked for. Once a rank delivers design
    documents, the planner re-derives the remaining subsystems' items from them (rank_breakdown) and
    the next rank is built from that breakdown, recorded once per planner session."""
    import json
    from common import budget_gate
    _iso(monkeypatch, tmp_path)
    repo = _repo(tmp_path)
    os.makedirs(os.path.join(repo, "docs/design"))
    with open(os.path.join(repo, "docs/design/architecture.md"), "w") as fh:
        fh.write("# resp: reader.go, writer.go, errors.go only\n")
    git.git(repo, "add", "-A")
    git.git(repo, "commit", "-m", "design")
    subs = [
        {"id": "design", "deliverable": "docs", "files": ["docs/design/architecture.md"]},
        {"id": "resp", "files": ["resp/value.go"], "dependsOn": ["design"],
         "items": [{"id": "r-value", "files": ["resp/value.go"]}]},
        {"id": "server", "files": ["server/server.go"], "dependsOn": ["resp"]},
    ]
    pid = "prog-bd"
    program_rank_init(fake_task_input(correlationId=pid, plan={"subtasks": subs, "contractPaths": ["go.mod"]}))
    assert program_rank_build(fake_task_input(correlationId=pid, ceilingUsd=10)).output_data["rank"] == ["design"]
    ap = program_apply_rank(fake_task_input(
        correlationId=pid, repoPath=repo, rank=["design"], description="a RESP cache", memoryDigest="prior: go",
        joined={"team_design__1": {"output": {"delivered": True, "verdict": "DELIVERED"}}})).output_data
    assert ap["breakdown"] == "yes" and ap["designDocs"] == ["docs/design/architecture.md"]
    assert ap["breakdownSchema"] == program.BREAKDOWN_SCHEMA
    p = ap["breakdownPrompt"]
    assert "docs/design/architecture.md" in p and "a RESP cache" in p and "prior: go" in p
    assert '"id": "resp"' in p and '"id": "server"' in p and '"id": "design"' not in p and "go.mod" in p

    # The breakdown (the agent's output) is folded into the next rank build: new items, new files,
    # spend recorded once, a resolved notice per re-sliced subsystem.
    agent_out = {"sessionId": "sess-bd", "costUsd": 1.25, "tokenUsed": 900, "structured": {"subsystems": [
        {"id": "resp", "items": [
            {"id": "r-reader", "files": ["resp/reader.go"], "acceptanceCriteria": ["parses arrays"]},
            {"id": "r-writer", "files": ["resp/writer.go"], "dependsOn": ["r-reader", "ghost"]},
            {"id": "", "files": ["resp/x.go"]}, {"id": "r-empty", "files": []}]},
        {"id": "server", "items": [{"id": "srv", "files": ["server/server.go", "server/conn.go"]}]},
        {"id": "unknown", "items": [{"id": "u", "files": ["u.go"]}]}]}}
    rb = program_rank_build(fake_task_input(correlationId=pid, ceilingUsd=10, breakdown=agent_out)).output_data
    assert rb["rank"] == ["resp"] and rb["resliced"] == ["resp", "server"]
    items = rb["dynamicTasksInput"]["team_resp"]["subtasks"]
    assert [x["id"] for x in items] == ["r-reader", "r-writer"] and items[1]["dependsOn"] == ["r-reader"]
    board = tb.fold_board(pid)
    assert board["items"]["resp"]["files"] == ["resp/reader.go", "resp/writer.go"]
    assert board["items"]["server"]["files"] == ["server/conn.go", "server/server.go"]
    notices = [e for e in board["exceptions"] if e["kind"] == "breakdown"]
    assert {e["item"] for e in notices} == {"resp", "server"} and "r-reader, r-writer" in notices[0]["detail"]
    assert tb.open_exceptions(board["exceptions"]) == []  # notices for awareness: nothing waits on them
    assert budget_gate.read_spend(pid)["usd"] == 1.25
    # The loop hands the same output to every later rank build: folded once.
    rb_again = program_rank_build(fake_task_input(correlationId=pid, ceilingUsd=10, breakdown=agent_out)).output_data
    assert rb_again["resliced"] == [] and budget_gate.read_spend(pid)["usd"] == 1.25
    assert [x["id"] for x in rb_again["dynamicTasksInput"]["team_resp"]["subtasks"]] == ["r-reader", "r-writer"]
    # No design documents in a rank: no breakdown asked for.
    ap2 = program_apply_rank(fake_task_input(correlationId=pid, repoPath=repo, rank=["resp"], joined={
        "team_resp__2": {"output": {"delivered": True, "verdict": "DELIVERED"}}})).output_data
    assert ap2["breakdown"] == "no" and ap2["breakdownPrompt"] == ""

    wf = json.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "cc_program.json")))
    loop = next(t for t in wf["tasks"] if t["taskReferenceName"] == "rank_loop")["loopOver"]
    assert [t["taskReferenceName"] for t in loop] == ["rank_build", "rank_fan_out", "rank_join", "apply_rank",
                                                      "breakdown_gate", "capture_rank"]
    gate = loop[4]
    assert gate["inputParameters"] == {"breakdown": "${apply_rank.output.breakdown}"}
    agent = gate["decisionCases"]["yes"][0]
    assert agent["name"] == "coding_agent" and agent["optional"] is True
    assert agent["inputParameters"]["worktreePath"] == "${workspace.output.worktreePath}"
    assert agent["inputParameters"]["prompt"] == "${apply_rank.output.breakdownPrompt}"
    assert loop[5]["inputParameters"]["breakdown"] == "${rank_breakdown.output}"
    assert loop[0]["inputParameters"]["breakdown"] == "${workflow.variables.breakdown}"


def test_nested_program_recurses_within_max_depth(monkeypatch, tmp_path, fake_task_input):
    _iso(monkeypatch, tmp_path)
    subs = [{"id": "core", "files": ["core/a.py"], "subsystems": [{"id": "a", "files": ["core/a.py"]}]}]
    program_rank_init(fake_task_input(correlationId="p", plan={"subtasks": subs}))
    rb = program_rank_build(fake_task_input(correlationId="p", maxDepth=2, ceilingUsd=10)).output_data
    assert rb["dynamicTasks"][0]["subWorkflowParam"]["name"] == "cc_program"
    assert rb["dynamicTasksInput"]["program_core"]["depth"] == 1
    _iso(monkeypatch, tmp_path / "b")
    program_rank_init(fake_task_input(correlationId="p2", plan={"subtasks": subs}, depth=1))
    rb = program_rank_build(fake_task_input(correlationId="p2", maxDepth=2, ceilingUsd=10)).output_data
    assert rb["dynamicTasks"][0]["subWorkflowParam"]["name"] == "team_build"  # depth bound reached


def test_rejected_plan_never_builds(fake_task_input, monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    out = program_rank_init(fake_task_input(correlationId="pr", plan={"route": "replan", "replanReasons": ["cycle"],
                                                                      "subtasks": SUBS})).output_data
    assert out["gate"] == "rejected" and out["reasons"] == ["cycle"]


def _repo(tmp_path):
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "seed.py"), "w") as fh:
        fh.write("x = 1\n")
    git.git(shared, "add", "-A")
    git.git(shared, "commit", "-m", "base")
    return shared


def test_child_teams_share_program_scoped_ownership_and_parent_directives(monkeypatch, tmp_path, fake_task_input):
    _iso(monkeypatch, tmp_path)
    shared = _repo(tmp_path)
    pid = "prog-own"
    for team, item in (("team-a", "x"), ("team-b", "y")):
        team_plan_init(fake_task_input(correlationId=team, contractPaths=[], parentCorrelationId=pid,
                                       subsystemId=team, subtasks=[{"id": item, "files": ["shared.py"]}]))
    tb.post_directive(pid, "human", "subsystem:team-b", "use the v2 schema", authority="human")
    a = team_standup(fake_task_input(repoPath=shared, correlationId="team-a", parentCorrelationId=pid,
                                     subsystemId="team-a")).output_data
    b = team_standup(fake_task_input(repoPath=shared, correlationId="team-b", parentCorrelationId=pid,
                                     subsystemId="team-b")).output_data
    assert a["groupIds"] == "x" and b["groupIds"] == ""  # program-scoped: team-b waits for shared.py
    assert file_ownership.owner_map(pid) == {"shared.py": "team-a:x"}
    file_ownership.release(pid, "team-a:x", "shared.py")
    b = team_standup(fake_task_input(repoPath=shared, correlationId="team-b", parentCorrelationId=pid,
                                     subsystemId="team-b")).output_data
    assert b["groupIds"] == "y" and "use the v2 schema" in b["dynamicTasksInput"]["y"]["prompt"]


def test_concurrent_integrators_on_one_branch_stay_linear(tmp_path):
    shared = _repo(tmp_path)
    branch = git._current_branch(shared)
    items = []
    for n in range(6):
        wt = os.path.join(shared, git.WORKTREES, f"f{n}")
        git.git(shared, "worktree", "add", wt, "-b", f"cc-group-f{n}", branch)
        with open(os.path.join(wt, f"f{n}.py"), "w") as fh:
            fh.write(f"N = {n}\n")
        git.git(wt, "add", "-A")
        git.git(wt, "commit", "-m", f"f{n}")
        items.append({"id": f"f{n}", "worktree": wt, "branch": f"cc-group-f{n}"})
    results = []
    threads = [threading.Thread(target=lambda it=it: results.append(
        continuous_integrate.integrate_ready(shared, branch, [it]))) for it in items]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(len(r["integrated"]) for r in results) == 6
    log = git.git(shared, "log", "--format=%P", branch).stdout.split("\n")
    assert all(len(p.split()) <= 1 for p in log)  # no merge commit: strictly linear
    assert all(os.path.exists(os.path.join(shared, f"f{n}.py")) for n in range(6))


def test_child_reviewer_sees_program_directives(monkeypatch, tmp_path, fake_task_input):
    # Live-found (run dcc695a6): the child team's reviewer, blind to a program-board human directive,
    # rejected the work three times for following it.
    from team.tasks import team_review_build
    _iso(monkeypatch, tmp_path)
    shared = _repo(tmp_path)
    pid = "prog-rev"
    team_plan_init(fake_task_input(correlationId="t-rep", contractPaths=[], parentCorrelationId=pid,
                                   subsystemId="report", subtasks=[{"id": "report", "files": ["report.py"]}]))
    tb.post_directive(pid, "human:ops@example.com", "subsystem:report", "every line must start with '- '",
                      authority="human")
    tb.set_state("t-rep", "i", "report", tb.REVIEW)
    rb = team_review_build(fake_task_input(sharedRepo=shared, correlationId="t-rep", reviewing=[],
                                          finalHead=git.head(shared), parentCorrelationId=pid,
                                          subsystemId="report")).output_data
    p = rb["dynamicTasksInput"]["review_report"]["prompt"]
    assert "(human) every line must start with '- '" in p and "OVERRIDES" in p


def test_breakdown_criteria_written_as_text_become_a_list_and_the_schema_requires_lists():
    """The breakdown typed its items as bare objects, so a planner's paragraph passed validation as
    acceptanceCriteria and the fold dropped it: no frozen test anywhere in that run (run 90e5e3aa)."""
    from common import program
    subs = [{"id": "server", "dependsOn": [], "items": [{"id": "old", "files": ["x.go"]}]}]
    bd = {"subsystems": [{"id": "server", "items": [
        {"id": "cmd-dispatch", "description": "d", "files": ["internal/command/command.go"], "dependsOn": [],
         "acceptanceCriteria": "Registry wires every handler.\nDispatch is case-insensitive."}]}]}
    out, changed = program.apply_breakdown(subs, bd)
    item = out[0]["items"][0]
    assert changed == ["server"]
    assert item["acceptanceCriteria"] == ["Registry wires every handler.", "Dispatch is case-insensitive."]
    assert item["reviewCriteria"] == []
    child = program.child_items({"id": "s", "items": [{"id": "i", "files": ["f"], "acceptanceCriteria": "one criterion"}]})
    assert child[0]["acceptanceCriteria"] == ["one criterion"]
    item_schema = program.BREAKDOWN_SCHEMA["properties"]["subsystems"]["items"]["properties"]["items"]["items"]
    assert item_schema["properties"]["acceptanceCriteria"]["type"] == "array"
    assert item_schema["properties"]["files"]["type"] == "array"
    assert {"id", "description", "files", "acceptanceCriteria"} <= set(item_schema["required"])


def test_breakdown_prompt_asks_for_each_source_files_test_file():
    """sv-registry's slice named registry.go but not registry_test.go; its implementer was refused the
    test file and the Lead widened the item a review round later (run 688f426b). The breakdown asks for
    the test file alongside every source file from the start."""
    from common import program
    p = program.breakdown_prompt(description="d", docs=["docs/design/architecture.md"],
                                 remaining=[{"id": "store", "files": ["store/store.go"]}], contract_paths=[])
    assert "a source file's test file alongside it" in p and "foo_test.go" in p


def test_an_incomplete_team_is_re_run_once_with_its_diagnosis(monkeypatch, tmp_path, fake_task_input):
    """pgtype delivered both items but was refused at its final gate for the parser team's tests, still
    being built in the same rank; its repair agent said so, and the program simply ended at 2/9 (run
    037e1cbe). The program now re-runs an INCOMPLETE team once its rank is done, carrying the team's own
    report as a directive, and re-forks only that team; past maxTeamAttempts it blocks as before."""
    _iso(monkeypatch, tmp_path)
    pid = "prog-retry"
    init = program_rank_init(fake_task_input(correlationId=pid, plan={"subtasks": SUBS, "contractPaths": []},
                                             maxTeamAttempts=2)).output_data
    assert init["maxRankIterations"] == 6  # 3 ranks, each may run twice
    program_rank_build(fake_task_input(correlationId=pid, ceilingUsd=30))
    program_apply_rank(fake_task_input(correlationId=pid, rank=["model"], joined={
        "team_model__1": {"output": {"delivered": True, "verdict": "DELIVERED"}}}))
    program_rank_build(fake_task_input(correlationId=pid, ceilingUsd=30))
    report = "INCOMPLETE: shared branch not green; the final test cycle ran no command for test_accept_s1.py"
    ap = program_apply_rank(fake_task_input(correlationId=pid, rank=["api", "store"], joined={
        "team_api__2": {"output": {"delivered": True, "verdict": "DELIVERED"}},
        "team_store__2": {"output": {"delivered": False, "verdict": "INCOMPLETE", "report": report,
                                     "spendUsd": 2.0}}})).output_data
    assert ap["continue"] is True and ap["retrying"] == ["store"] and ap["rankDelivered"] is False
    board = tb.fold_board(pid)
    assert board["items"]["store"]["state"] == tb.REOPENED and board["items"]["store"]["attempt"] == 1
    note = [d for d in board["directives"] if d.get("target") == "subsystem:store"][-1]
    assert report in note["text"] and "already on the branch" in note["text"]
    assert tb.open_exceptions(board["exceptions"]) == []  # a decision recorded, nothing waits
    # The re-run forks only the unfinished team, with the remaining budget to itself.
    rb = program_rank_build(fake_task_input(correlationId=pid, ceilingUsd=30)).output_data
    assert rb["rank"] == ["api", "store"] and list(rb["dynamicTasksInput"]) == ["team_store"]
    assert rb["childBudgetUsd"] == 28.0
    # The re-run team reads the diagnosis through its program's directives.
    from team.tasks import _parent_directives
    texts = [d["text"] for d in _parent_directives({"parentCorrelationId": pid, "subsystemId": "store"})]
    assert any(report in t for t in texts)
    # Its second outcome decides: api (delivered earlier, not re-run) stays done.
    ap2 = program_apply_rank(fake_task_input(correlationId=pid, rank=["api", "store"], joined={
        "team_store__3": {"output": {"delivered": True, "verdict": "DELIVERED"}}})).output_data
    assert ap2["continue"] is True and ap2["rankDelivered"] is True and ap2["retrying"] == []
    assert tb.fold_board(pid)["items"]["api"]["state"] == tb.DONE
    # A second failure of the same team, past the cap, blocks it as before.
    tb.set_state(pid, "program", "cli", tb.IN_PROGRESS, attempt=1)
    ap3 = program_apply_rank(fake_task_input(correlationId=pid, rank=["cli"], joined={
        "team_cli__4": {"output": {"delivered": False, "verdict": "INCOMPLETE", "report": "still red"}}})).output_data
    assert ap3["continue"] is False and tb.fold_board(pid)["items"]["cli"]["state"] == tb.BLOCKED
    import json as _j
    wf = _j.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "cc_program.json")))
    assert wf["inputTemplate"]["maxTeamAttempts"] == 2
    def walk(ts):
        for t in ts:
            yield t
            for k in ("loopOver", "defaultCase"):
                yield from walk(t.get(k) or [])
            for c in (t.get("decisionCases") or {}).values():
                yield from walk(c)
    loop = [t for t in walk(wf["tasks"]) if t.get("taskReferenceName") == "rank_loop"][0]
    assert "$.max_iterations" in loop["loopCondition"]
    assert loop["inputParameters"]["max_iterations"] == "${rank_init.output.maxRankIterations}"
