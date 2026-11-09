"""Docs deliverables, run retrospectives → learnings, live activity, and run-scoped fork branches."""
import json
import os

import common  # noqa: F401
from common import feedback, git, retrospective as retro, team_board as tb
from stages.tasks import repo_memory_read, retro_evidence, retro_record
from common import blackboard as bb
from team.tasks import team_acceptance_build, team_plan_init, team_review_build, team_standup

WF = os.path.join(os.path.dirname(__file__), "..", "workflows")


def _iso(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))


def _repo(tmp_path, name="shared", files=None):
    path = str(tmp_path / name)
    git.ensure_ready(path)
    for rel, text in (files or {"go.mod": "module x\n", "main.go": "package main\n"}).items():
        with open(os.path.join(path, rel), "w") as fh:
            fh.write(text)
    git.git(path, "add", "-A")
    git.git(path, "commit", "-m", "base")
    return path


# ─────────────────────────── (a) documents are a deliverable too ──────────────
def test_deliverable_is_declared_or_inferred_from_files():
    assert tb.deliverable_of({"deliverable": "design", "files": ["x.go"]}) == "docs"
    assert tb.deliverable_of({"deliverable": "code", "files": ["docs/a.md"]}) == "code"
    assert tb.deliverable_of({"files": ["docs/design/architecture.md", "docs/adr/001.md"]}) == "docs"
    assert tb.deliverable_of({"files": ["docs/a.md", "store/store.go"]}) == "code"
    assert tb.deliverable_of({"files": []}) == "code"


def test_docs_item_gets_a_documents_prompt_no_acceptance_tests_and_a_docs_reviewer(monkeypatch, tmp_path,
                                                                                  fake_task_input):
    _iso(monkeypatch, tmp_path)
    shared = _repo(tmp_path)
    cid = "run-docs"
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {"id": "design", "description": "architecture spec", "files": ["docs/design/architecture.md"],
         "acceptanceCriteria": ["names every package and its interface"]},
        {"id": "store", "description": "keyspace", "deliverable": "code", "files": ["store.go"]}]))
    assert tb.fold_board(cid)["items"]["design"]["deliverable"] == "docs"
    acc = team_acceptance_build(fake_task_input(correlationId=cid, repoPath=shared,
                                                authorAcceptanceTests=True)).output_data
    assert "design" not in json.dumps(acc.get("dynamicTasksInput") or {})  # no executable oracle for docs
    su = team_standup(fake_task_input(repoPath=shared, correlationId=cid, maxWidth=4)).output_data
    assert "DELIVERABLE: DOCUMENTS, not code" in su["dynamicTasksInput"]["design"]["prompt"]
    assert "DELIVERABLE: DOCUMENTS" not in su["dynamicTasksInput"]["store"]["prompt"]
    tb.set_state(cid, "integrator", "design", tb.REVIEW)
    rv = team_review_build(fake_task_input(correlationId=cid, sharedRepo=shared,
                                           sharedBranch=git._current_branch(shared))).output_data
    prompt = next(v["prompt"] for v in rv["dynamicTasksInput"].values())
    assert "deliverable is DOCUMENTS" in prompt and "you are the only oracle" in prompt


# ─────────────────────────── (b) retrospective → learnings ────────────────────
def test_learnings_are_scoped_deduplicated_retractable_and_relevant(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    written = retro.record([
        {"scope": "repo", "text": "store tests need -race", "evidence": "run x"},
        {"scope": "environment", "text": "go 1.26 at /opt/homebrew/bin/go", "evidence": "go version"},
        {"scope": "language", "language": "go", "text": "Go: standard library only", "evidence": "AGENTS.md"},
        {"scope": "process", "text": "split command families after the dispatch core", "evidence": "replan"},
        {"scope": "repo", "text": "Store tests need -race!", "evidence": "dup"},
        {"scope": "bogus", "text": "dropped", "evidence": ""}],
        repo="git@x:resp-cache", languages=["go"], host_name="hostA", run="r1")
    assert [w["scope"] for w in written] == ["repo", "environment", "language", "process"]
    rel = retro.relevant(repo="git@x:resp-cache", languages=["go"], host_name="hostA")
    assert [x["scope"] for x in rel] == ["repo", "environment", "language", "process"]
    # Another repo / host / language sees only what applies to it.
    other = retro.relevant(repo="git@x:other", languages=["java"], host_name="hostB")
    assert [x["text"] for x in other] == ["split command families after the dispatch core"]
    retro.retract("split command families after the dispatch core", by="human:ops@example.com")
    assert retro.relevant(repo="git@x:other", languages=["java"], host_name="hostB") == []
    assert "[language:go] Go: standard library only" in retro.digest(rel)


def test_repo_key_of_a_worktree_is_its_main_checkout(tmp_path):
    main = _repo(tmp_path, "main")
    wt = git.worktree_add(main, "run-1")["worktreePath"]
    assert retro.repo_key(wt) == retro.repo_key(main) == os.path.realpath(main)
    assert retro.detect_languages(main) == ["go"]


def test_retrospective_workers_gather_evidence_record_and_feed_the_next_run(monkeypatch, tmp_path,
                                                                            fake_task_input):
    _iso(monkeypatch, tmp_path)
    shared = _repo(tmp_path)
    cid = "run-retro"
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[{"id": "a", "files": ["a.go"]}]))
    tb.set_state(cid, "integrator", "a", tb.BLOCKED, reason="go vet failed: unused import")
    tb.post_directive(cid, "human:ops@example.com", "team", "use the standard library only", authority="human")
    # A child team of a program is covered by the program's retrospective.
    assert retro_evidence(fake_task_input(correlationId=cid, repoPath=shared, verdict="INCOMPLETE",
                                          parentCorrelationId="prog-1")).output_data["skip"] == "yes"
    ev = retro_evidence(fake_task_input(correlationId=cid, repoPath=shared, verdict="INCOMPLETE",
                                        report="1 blocked")).output_data
    assert ev["skip"] == "no" and ev["languages"] == ["go"] and ev["repoKey"] == os.path.realpath(shared)
    assert "go vet failed: unused import" in ev["prompt"] and "use the standard library only" in ev["prompt"]
    assert ev["schema"]["properties"]["learnings"]
    out = retro_record(fake_task_input(
        correlationId=cid, repoKey=ev["repoKey"], languages=ev["languages"], host=ev["host"],
        retro={"structured": {"summary": "vet blocked a", "learnings": [
            {"scope": "language", "text": "Go: run go vet before committing; unused imports fail the build",
             "evidence": "a blocked by go vet"},
            {"scope": "environment", "text": "go 1.26 is installed via Homebrew", "evidence": "go version"}]}})).output_data
    assert out["recorded"] == 2 and out["byScope"] == {"language": 1, "environment": 1}
    assert bb.read_facts(cid, kind="retrospective")[-1]["data"]["summary"] == "vet blocked a"
    # The next run on this repo starts from them: planner digest and every implementer prompt.
    digest = repo_memory_read(fake_task_input(repoPath=shared)).output_data["digest"]
    assert "unused imports fail the build" in digest and "Homebrew" in digest
    nxt = "run-next"
    team_plan_init(fake_task_input(correlationId=nxt, contractPaths=[], subtasks=[{"id": "b", "files": ["b.go"]}]))
    su = team_standup(fake_task_input(repoPath=shared, correlationId=nxt, maxWidth=2)).output_data
    p = su["dynamicTasksInput"]["b"]["prompt"]
    assert "Learnings from earlier runs" in p and "unused imports fail the build" in p


_MANGLED = ('Run ended PROGRAM_INCOMPLETE at 2/6 subsystems; resp blocked.</summary>\n<learnings>'
            '[{"scope":"repo","text":"The resp package must not contain a value.go or any exported Value type: '
            'resp.md R.2 forbids a unified RESP2 value type.","evidence":"review of r-value","confidence":"high"},'
            '{"scope":"process","text":"Slice the resp package as r-reader/r-writer/r-errors per architecture.md.",'
            '"evidence":"r-value contradicted the design"}]</learnings>')


def test_retrospective_salvages_the_real_learnings_from_a_rejected_structured_output(monkeypatch, tmp_path,
                                                                                    fake_task_input):
    """Run a062184f: the agent's StructuredOutput payloads put the learnings inside the summary string as
    tagged text, the validator rejected all of them, and the agent finally sent {"summary": "test",
    "learnings": [{"text": "a"}]} — which was recorded. The rejected payloads are untangled and the
    one with real learnings is recorded instead; a placeholder is never a learning."""
    _iso(monkeypatch, tmp_path)
    fixed = retro.untangle({"summary": _MANGLED})
    assert fixed["summary"] == "Run ended PROGRAM_INCOMPLETE at 2/6 subsystems; resp blocked."
    assert [x["scope"] for x in fixed["learnings"]] == ["repo", "process"]
    assert retro.untangle({"summary": "plain", "learnings": []}) == {"summary": "plain", "learnings": []}
    assert retro.substantive([{"scope": "repo", "text": "a"}, {"scope": "nope", "text": "x" * 40}]) == []

    out = retro_record(fake_task_input(
        correlationId="run-salvage", repoKey="/r", languages=["go"], host="h",
        retro={"structured": {"summary": "test", "learnings": [{"scope": "repo", "text": "a", "evidence": "b"}]},
               "result": '{"summary":"test","learnings":[{"scope":"repo","text":"a","evidence":"b"}]}',
               "structuredAttempts": [{"summary": _MANGLED}, {"file_path": "dummy"},
                                      {"summary": "test", "learnings": [{"scope": "repo", "text": "a", "evidence": "b"}]}]})).output_data
    assert out["recorded"] == 2 and out["byScope"] == {"repo": 1, "process": 1}
    assert out["summary"] == "Run ended PROGRAM_INCOMPLETE at 2/6 subsystems; resp blocked."
    assert all(len(x["text"]) >= retro.MIN_LEARNING_CHARS for x in retro.everything())
    # The accepted output wins when it is the substantive one.
    real = {"summary": "s", "learnings": [{"scope": "environment", "text": "go 1.26 is installed via Homebrew",
                                           "evidence": "go version"}]}
    assert retro.salvage([real, {"summary": _MANGLED}]) == real


def test_retrospective_agent_reads_the_runs_workspace_not_the_source_checkout(monkeypatch, tmp_path,
                                                                             fake_task_input):
    """Run a062184f's retrospective agent ran in the source checkout (branch main) and saw 'only the
    scaffold': the run's work is on its workspace branch. The workspace is handed over, or read back
    from the run's own workspace task when the failure workflow did not receive it."""
    import stages.tasks as st
    _iso(monkeypatch, tmp_path)
    repo = _repo(tmp_path)
    ws = git.worktree_add(repo, "run-x")["worktreePath"]
    cid = "run-ws"
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[{"id": "a", "files": ["a.go"]}]))
    ev = retro_evidence(fake_task_input(correlationId=cid, repoPath=repo, workspacePath=ws, verdict="INCOMPLETE",
                                        report="x")).output_data
    assert ev["worktreePath"] == ws and ev["repoPath"] == repo
    monkeypatch.setattr(st, "_conductor_workflow", lambda wid, tasks=False: {
        "input": {"repoPath": repo}, "output": {"verdict": "FAILED"},
        "tasks": [{"referenceTaskName": "workspace", "outputData": {"worktreePath": ws}}] if tasks else []})
    ev2 = retro_evidence(fake_task_input(correlationId=cid, report="failed")).output_data
    assert ev2["worktreePath"] == ws
    ev3 = retro_evidence(fake_task_input(correlationId=cid, repoPath=repo, workspacePath=str(tmp_path / "gone"),
                                         verdict="INCOMPLETE", report="x")).output_data
    assert ev3["worktreePath"] == repo  # a workspace that no longer exists falls back to the checkout
    d = json.load(open(os.path.join(WF, "run_retrospective.json")))
    assert "workspacePath" in d["inputParameters"]
    agent = d["tasks"][2]["decisionCases"]["no"][0]
    assert agent["inputParameters"]["worktreePath"] == "${evidence.output.worktreePath}"
    for name in ("team_build", "cc_program"):
        w = json.load(open(os.path.join(WF, f"{name}.json")))
        assert w["tasks"][-1]["inputParameters"]["workspacePath"] == "${workspace.output.worktreePath}"


def test_every_swarm_run_ends_with_a_retrospective_and_names_it_as_failure_workflow():
    for name in ("team_build", "cc_program"):
        d = json.load(open(os.path.join(WF, f"{name}.json")))
        assert d["failureWorkflow"] == "run_retrospective"
        last = d["tasks"][-1]
        assert last["type"] == "SUB_WORKFLOW" and last["subWorkflowParam"] == {"name": "run_retrospective"}
        assert last["optional"] is True and last["inputParameters"]["correlationId"] == "${workflow.workflowId}"
    r = json.load(open(os.path.join(WF, "run_retrospective.json")))
    assert [t["taskReferenceName"] for t in r["tasks"]] == ["model_policy", "evidence", "retro_gate"]


# ─────────────────────────── (c) live activity for state on demand ────────────
def test_implementer_posts_a_throttled_activity_heartbeat(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    w = feedback.FeedbackWatcher("run-act", "store", every=1, read=lambda cid: [])
    w.on_tool_call("Edit store/expiry.go")
    w.on_tool_call("Bash go test ./store")  # inside the interval: not posted again
    facts = bb.read_facts("run-act", kind="activity")
    assert len(facts) == 1 and facts[0]["data"] == {"item": "store", "toolCalls": 1, "tool": "Edit store/expiry.go"}
    w._activity_ts -= feedback.FeedbackWatcher.ACTIVITY_INTERVAL_S
    w.on_tool_call("Bash go test ./store")
    assert bb.read_facts("run-act", kind="activity")[-1]["data"]["toolCalls"] == 3


# ─────────────────────────── run 3e502ddc: store team mechanisms ──────────────
def test_each_items_gate_runs_only_its_own_acceptance_test(monkeypatch, tmp_path, fake_task_input):
    """Discovery batched all 8 store acceptance files into one pytest command and every item's gate
    inherited it: core's repair agent then implemented keys/hashes/lists/strings to turn them green."""
    from team.tasks import team_acceptance_commit
    _iso(monkeypatch, tmp_path)
    shared = _repo(tmp_path)
    cid = "run-own-gate"
    paths = {g: f"tests/acceptance/test_accept_{g}.py" for g in ("core", "keys", "hashes")}
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[],
                                   subtasks=[{"id": g, "files": [f"store/{g}.go"]} for g in paths]))
    os.makedirs(os.path.join(shared, "tests", "acceptance"))
    for p in paths.values():
        with open(os.path.join(shared, p), "w") as fh:
            fh.write("def test_x():\n    assert False\n")
    git.git(shared, "add", "-A")
    git.git(shared, "commit", "-m", "acceptance")
    run = {"verificationState": "failed",
           "commands": [{"argv": ["pytest", "-q", *paths.values()], "exitCode": 1}]}
    team_acceptance_commit(fake_task_input(correlationId=cid, repoPath=shared, authored=paths, run=run, joined={}))
    items = tb.fold_board(cid)["items"]
    for g, p in paths.items():
        assert items[g]["testCommands"] == [{"argv": ["pytest", "-q", p]}]


def test_no_commit_but_frozen_acceptance_tests_pass_is_done_not_blocked(monkeypatch, tmp_path, fake_task_input):
    from team.tasks import team_integrate
    _iso(monkeypatch, tmp_path)
    shared = _repo(tmp_path)
    branch = git._current_branch(shared)
    cid = "run-satisfied"
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[
        {"id": "hashes", "files": ["store/hashes.go"]}, {"id": "lists", "files": ["store/lists.go"]}]))
    for g in ("hashes", "lists"):
        tb.post_item(cid, "acceptance", {**tb.fold_board(cid)["items"][g],
                                         "acceptanceTests": [f"tests/acceptance/test_accept_{g}.py"]})
        git.worktree_add(shared, g)  # fork with no commit
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, contractPaths=[], groupIds="hashes,lists",
        dispatched=[{"id": "hashes", "files": ["store/hashes.go"], "attempt": 0},
                    {"id": "lists", "files": ["store/lists.go"], "attempt": 0}], maxAttempts=2,
        joined={"hashes": {"testsPassed": True}, "lists": {"testsPassed": False}})).output_data
    assert out["doneNow"] == ["hashes"] and [r["id"] for r in out["retry"]] == ["lists"]
    items = tb.fold_board(cid)["items"]
    assert items["hashes"]["state"] == tb.DONE and "already satisfied" in items["hashes"]["reason"]


def test_sandboxed_agents_get_a_writable_git_excluded_go_cache(tmp_path):
    from common.coding_agent import TOOL_CACHE_DIR, _tool_cache_env
    main = _repo(tmp_path, "main")
    wt = git.worktree_add(main, "keys")["worktreePath"]
    env = _tool_cache_env(wt)
    _tool_cache_env(wt)  # idempotent
    assert env["GOCACHE"].startswith(os.path.join(wt, TOOL_CACHE_DIR)) and os.path.isdir(env["GOCACHE"])
    with open(os.path.join(env["GOCACHE"], "blob"), "w") as fh:
        fh.write("x")
    assert git.git(wt, "status", "--porcelain").stdout.strip() == ""
    exclude = git.git(wt, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    path = exclude if os.path.isabs(exclude) else os.path.join(wt, exclude)
    assert open(path).read().count(f"/{TOOL_CACHE_DIR}/") == 1


def _amendment_setup(monkeypatch, tmp_path, fake_task_input, cid):
    from common import contract_event
    from team.tasks import team_integrate
    _iso(monkeypatch, tmp_path)
    test = "tests/acceptance/test_accept_keys.py"
    shared = _repo(tmp_path, files={"go.mod": "module x\n", "keys.go": "package x\n"})
    os.makedirs(os.path.join(shared, "tests", "acceptance"))
    with open(os.path.join(shared, test), "w") as fh:
        fh.write("assert exists_after_del == 2\n")
    git.git(shared, "add", "-A")
    git.git(shared, "commit", "-m", "frozen acceptance")
    branch = git._current_branch(shared)
    team_plan_init(fake_task_input(correlationId=cid, contractPaths=[], subtasks=[{"id": "keys", "files": ["keys.go"]}]))
    tb.post_item(cid, "acceptance", {**tb.fold_board(cid)["items"]["keys"], "acceptanceTests": [test]})
    bb.post_fact(cid, "lead", contract_event.CONTRACT_KIND, {"paths": [test], "frozen": True, "revision": 1,
                                                             "acceptanceTests": [test], "acceptanceCommit": git.head(shared)})
    wt = git.worktree_add(shared, "keys")["worktreePath"]
    for rel, text in (("keys.go", "package x\n// per-key Del\n"), (test, "assert exists_after_del == 0\n")):
        with open(os.path.join(wt, rel), "w") as fh:
            fh.write(text)
    git.git(wt, "add", "-A")
    git.git(wt, "commit", "-m", "keys + amend wrong acceptance assertion")
    out = team_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=branch, correlationId=cid, contractPaths=[test], groupIds="keys",
        enablePeerReview=True, dispatched=[{"id": "keys", "files": ["keys.go"], "attempt": 0}], joined={})).output_data
    return shared, branch, test, out


def test_wrong_frozen_acceptance_test_is_amended_when_the_reviewer_approves(monkeypatch, tmp_path, fake_task_input):
    """Run 3e502ddc: test_accept_keys asserted Exists==2 right after deleting both keys. Every Lead
    auto-reject left keys blocked forever; now the amendment is held unmerged and the reviewer decides."""
    from team.tasks import team_apply_reviews, team_outcome, team_review_build
    cid = "run-amend"
    shared, branch, test, out = _amendment_setup(monkeypatch, tmp_path, fake_task_input, cid)
    assert out["reviewing"] == ["keys"] and out["contractEvents"] == []
    assert "== 2" in open(os.path.join(shared, test)).read()  # nothing merged yet
    rv = team_review_build(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch)).output_data
    prompt = rv["dynamicTasksInput"]["review_keys"]["prompt"]
    assert "AMENDS FROZEN FILE(S)" in prompt and "== 0" in prompt
    ap = team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch,
                                            reviewed=["keys"], joined={"review_keys": {"output": {
                                                "verdict": "lgtm", "findings": []}}})).output_data
    assert ap["doneCount"] == 1 and tb.fold_board(cid)["items"]["keys"]["state"] == tb.DONE
    assert "== 0" in open(os.path.join(shared, test)).read()
    # The final cycle must have RUN the (amended, re-frozen) test at the delivered head (run 299b4907).
    oc = team_outcome(fake_task_input(correlationId=cid, repoPath=shared, finalHead=git.head(shared),
                                      testsPassed=True, testCycleState="tests_passed",
                                      testCommandsRan=[{"argv": ["pytest", "-q", test], "exitCode": 0}])).output_data
    assert oc["acceptanceTampered"] == [] and oc["acceptanceUnproven"] == [] and oc["verdict"] == "DELIVERED"


def test_rejected_acceptance_amendment_is_discarded_and_the_item_reopened(monkeypatch, tmp_path, fake_task_input):
    from team.tasks import team_apply_reviews, team_outcome
    cid = "run-amend-no"
    shared, branch, test, _ = _amendment_setup(monkeypatch, tmp_path, fake_task_input, cid)
    team_apply_reviews(fake_task_input(correlationId=cid, sharedRepo=shared, sharedBranch=branch, reviewed=["keys"],
                                       joined={"review_keys": {"output": {"verdict": "changes", "findings": [
                                           "the original test matches architecture.md; fix Del instead"]}}}))
    assert tb.fold_board(cid)["items"]["keys"]["state"] == tb.REOPENED
    assert "== 2" in open(os.path.join(shared, test)).read()
    assert git.git(shared, "branch", "--list", git.group_branch(shared, "keys")).stdout.strip() == ""
    # An edit that slips in later is still tampering.
    with open(os.path.join(shared, test), "w") as fh:
        fh.write("assert True\n")
    git.git(shared, "commit", "-am", "weaken")
    oc = team_outcome(fake_task_input(correlationId=cid, repoPath=shared, finalHead=git.head(shared),
                                      testsPassed=True, testCycleState="tests_passed")).output_data
    assert oc["acceptanceTampered"] == [test]


# ─────────────────────────── run-scoped fork branches ─────────────────────────
def test_two_runs_on_one_repo_never_claim_the_same_fork_branch(tmp_path):
    """Live failure (run 6d8af3af): a terminated run's worktree held 'cc-group-design', so the next run's
    'design' fork failed twice. Fork branches are scoped to each run's shared checkout."""
    main = _repo(tmp_path, "main")
    run1 = git.worktree_add(main, "run-1")["worktreePath"]
    run2 = git.worktree_add(main, "run-2")["worktreePath"]
    a = git.worktree_add(run1, "design")
    b = git.worktree_add(run2, "design")  # would fail with "already used by worktree" if unscoped
    assert a["branch"] != b["branch"] and a["branch"].endswith("-design") and b["branch"].endswith("-design")
    assert git.group_branch(run1, "design") == a["branch"]


def test_retrospective_evidence_carries_engine_failure_reasons_across_the_run_tree(monkeypatch):
    """The board says 'fork produced no commit'; the engine says why. Without the reason a live
    retrospective misdiagnosed a fork-branch collision (run 6d8af3af)."""
    import stages.tasks as st
    tree = {
        "prog": {"tasks": [{"referenceTaskName": "rank", "taskType": "SUB_WORKFLOW", "status": "COMPLETED",
                            "subWorkflowId": "team"}]},
        "team": {"tasks": [{"referenceTaskName": "design__1", "taskType": "SUB_WORKFLOW",
                            "status": "COMPLETED_WITH_ERRORS", "subWorkflowId": "fork",
                            "reasonForIncompletion": "Sub workflow code_subtask failed"}]},
        "fork": {"tasks": [{"referenceTaskName": "wt", "taskType": "SIMPLE", "status": "FAILED",
                            "reasonForIncompletion": "worktree_add: fatal: 'cc-group-design' is already used by worktree"}]},
    }
    monkeypatch.setattr(st, "_conductor_workflow", lambda wid, tasks=False: tree.get(wid, {}))
    reasons = [f["reason"] for f in st._task_failures("prog")]
    assert any("already used by worktree" in r for r in reasons) and len(reasons) == 2
