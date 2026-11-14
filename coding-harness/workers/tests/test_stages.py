"""Phase 4 stages: sdlc_intake routing (G1) and bug_fix repro-first logic (G2).

Covers the pure decisions (``common/intake.py``, ``common/bug_fix.py``) and the thin workers:
classification→route, the red/green repro gate, and the honest FIXED/NOT_FIXED/CANNOT_REPRODUCE
verdict including the H13 check-tamper guard.
"""
import os

import common  # noqa: F401 -- sys.path shim

from stages.tasks import (bootstrap_init, bootstrap_outcome, bug_adopt, bug_outcome,
                          bug_repro_gate, intake_route)
from common import bootstrap, bug_fix, git, intake


# ─────────────────────────── intake routing (G1) ───────────────────────────────
def test_route_by_kind_and_mode():
    assert intake.decide_route("bug") == "bug"
    assert intake.decide_route("greenfield") == "greenfield"
    assert intake.decide_route("feature", item_count=3) == "team"
    assert intake.decide_route("feature", item_count=1) == "single"
    assert intake.decide_route("refactor", item_count=0) == "single"
    assert intake.decide_route("feature", item_count=1, force_team=True) == "team"


def test_plan_route_drops_contract_paths_that_are_item_files():
    # A planner that freezes a file a subsystem must build would block every edit (H4/H14). plan_route
    # drops any contract path that is also an item file, keeping the plan buildable.
    plan = intake.plan_route({
        "kind": "feature", "confidence": 0.9,
        "subtasks": [{"id": "model", "files": ["model.py"]}, {"id": "svc", "files": ["service.py"]}],
        "contractPaths": ["test_program.py", "model.py", "service.py"],
    })
    assert plan["contractPaths"] == ["test_program.py"]


def test_plan_route_never_pauses_for_a_human():
    """Humans are passive observers: low confidence proceeds on the best classification, flagged."""
    low = {"kind": "feature", "confidence": 0.2,
           "subtasks": [{"id": "a", "files": ["x.py", "a.py"]}, {"id": "b", "files": ["x.py", "b.py"]}]}
    out = intake.plan_route(low)
    assert out["route"] == "team" and out["lowConfidence"] is True
    hi = {"kind": "bug", "confidence": 0.9, "instruction": "x"}
    assert intake.plan_route(hi)["route"] == "bug" and intake.plan_route(hi)["lowConfidence"] is False


def test_intake_route_worker_parses_coding_agent_shape(fake_task_input):
    # coding_agent returns the classification under result (a JSON string).
    out = intake_route(fake_task_input(
        classification={"result": '{"kind":"feature","confidence":0.9,"subtasks":[{"id":"a","files":["a.py"]},{"id":"b","files":["b.py"],"dependsOn":["a"]}],"instruction":"do x"}'})).output_data
    assert out["route"] == "team" and out["kind"] == "feature" and out["itemCount"] == 2


# ─────────────────────────── bug_fix repro gate (G2) ───────────────────────────
def test_repro_gate_classifies_red_green_blocked():
    assert bug_fix.repro_gate("failed")["reproduced"] is True
    assert bug_fix.repro_gate("passed")["outcome"] == "cannot_reproduce"
    assert bug_fix.repro_gate("blocked")["outcome"] == "blocked"


def test_bug_repro_gate_worker(fake_task_input):
    assert bug_repro_gate(fake_task_input(verificationState="failed")).output_data["reproduced"] is True
    assert bug_repro_gate(fake_task_input(verificationState="passed")).output_data["reproduced"] is False


# ─────────────────────────── bug_fix outcome + H13 guard ───────────────────────
def test_fix_outcome_verdicts():
    assert bug_fix.fix_outcome(reproduced=True, tests_passed=True)["verdict"] == "FIXED"
    assert bug_fix.fix_outcome(reproduced=True, tests_passed=False)["verdict"] == "NOT_FIXED"
    assert bug_fix.fix_outcome(reproduced=False, tests_passed=True)["verdict"] == "CANNOT_REPRODUCE"
    # Check tampering (H13): green under a modified repro test is NOT a fix.
    assert bug_fix.fix_outcome(reproduced=True, tests_passed=True, repro_test_tampered=True)["verdict"] == "NOT_FIXED"


def test_bug_outcome_worker_detects_test_tamper(tmp_path, fake_task_input):
    repo = str(tmp_path / "r")
    git.ensure_ready(repo)
    with open(os.path.join(repo, "test_bug_repro.py"), "w") as fh:
        fh.write("def test_bug():\n    assert compute() == 2\n")
    git.git(repo, "add", "-A"); git.git(repo, "commit", "-m", "repro red")
    repro_commit = git.head(repo)
    # The "fix" tampers with the repro test instead of fixing the bug.
    with open(os.path.join(repo, "test_bug_repro.py"), "w") as fh:
        fh.write("def test_bug():\n    assert True\n")
    git.git(repo, "add", "-A"); git.git(repo, "commit", "-m", "weaken test")
    head = git.head(repo)
    out = bug_outcome(fake_task_input(
        reproduced=True, testsPassed=True, testCycleState="tests_passed", repoPath=repo,
        reproCommit=repro_commit, finalHead=head, reproTestPaths=["test_bug_repro.py"])).output_data
    assert out["reproTestTampered"] is True and out["verdict"] == "NOT_FIXED"

    # An untouched repro test with green tests -> FIXED.
    out2 = bug_outcome(fake_task_input(
        reproduced=True, testsPassed=True, testCycleState="tests_passed", repoPath=repo,
        reproCommit=repro_commit, finalHead=repro_commit, reproTestPaths=["test_bug_repro.py"])).output_data
    assert out2["reproTestTampered"] is False and out2["verdict"] == "FIXED"


# ─────────────────────────── bug_adopt ff-merge ────────────────────────────────
def test_bug_adopt_fast_forwards_group_branch(tmp_path, fake_task_input):
    repo = str(tmp_path / "w")
    git.ensure_ready(repo)
    with open(os.path.join(repo, "a.py"), "w") as fh:
        fh.write("x = 1\n")
    git.git(repo, "add", "-A"); git.git(repo, "commit", "-m", "base")
    base_branch = git._current_branch(repo)
    # A code_subtask-style group branch with one commit ahead.
    wt = os.path.join(repo, git.WORKTREES, "repro")
    git.git(repo, "worktree", "add", wt, "-b", git.group_branch(repo, "repro"), base_branch)
    with open(os.path.join(wt, "test_bug_repro.py"), "w") as fh:
        fh.write("def test_x():\n    assert False\n")
    git.git(wt, "add", "-A"); git.git(wt, "commit", "-m", "repro")
    group_head = git.head(wt)

    out = bug_adopt(fake_task_input(sharedRepo=repo, groupId="repro")).output_data
    assert out["adopted"] is True and out["head"] == group_head
    # The workspace branch now contains the repro test (fast-forwarded, no merge commit).
    assert os.path.exists(os.path.join(repo, "test_bug_repro.py"))
    assert git.git(repo, "log", "--merges", "--oneline").stdout.strip() == ""


# ─────────────────────────── project_bootstrap (G3) ────────────────────────────
def test_bootstrap_outcome_verdicts():
    assert bootstrap.bootstrap_outcome(skeleton_green=False)["verdict"] == "SKELETON_NOT_GREEN"
    assert bootstrap.bootstrap_outcome(skeleton_green=True, feature_delivered=None)["verdict"] == "SKELETON_READY"
    assert bootstrap.bootstrap_outcome(skeleton_green=True, feature_delivered=True)["verdict"] == "BOOTSTRAPPED"
    assert bootstrap.bootstrap_outcome(skeleton_green=True, feature_delivered=False)["verdict"] == "FEATURE_INCOMPLETE"


def test_bootstrap_outcome_worker_none_feature(fake_task_input):
    out = bootstrap_outcome(fake_task_input(skeletonGreen=True, featureDelivered="none")).output_data
    assert out["verdict"] == "SKELETON_READY" and out["bootstrapped"] is True
    out2 = bootstrap_outcome(fake_task_input(skeletonGreen=True, featureDelivered=True)).output_data
    assert out2["verdict"] == "BOOTSTRAPPED"
    out3 = bootstrap_outcome(fake_task_input(skeletonGreen=False)).output_data
    assert out3["verdict"] == "SKELETON_NOT_GREEN" and out3["bootstrapped"] is False


def test_bootstrap_init_makes_empty_dir_git_ready(tmp_path, fake_task_input):
    d = str(tmp_path / "greenfield")
    out = bootstrap_init(fake_task_input(repoPath=d, firstFeature="add a CLI")).output_data
    assert out["baseCommit"] and out["hasFeature"] is True
    # It is now a real git repo with a base commit (so scaffold can commit on top).
    assert git.git(out["repoPath"], "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    assert bootstrap_init(fake_task_input(repoPath=d)).output_data["hasFeature"] is False


def test_a_program_in_a_fresh_directory_is_initialized_before_planning(tmp_path, fake_task_input):
    """Run edc1c893 was started on an empty directory: the planner failed four times on
    `git rev-parse HEAD` and the run ended before planning. The program's first step on the repo makes
    it a repository: an empty base commit, the directory's own files left exactly as they were."""
    from stages.tasks import repo_memory_read
    from common import git
    repo = tmp_path / "pg2"
    repo.mkdir()
    (repo / "notes.txt").write_text("keep me\n")
    out = repo_memory_read(fake_task_input(repoPath=str(repo))).output_data
    assert out["initializedRepo"] is True
    assert git.git(str(repo), "rev-parse", "--verify", "HEAD", check=False).code == 0
    assert (repo / "notes.txt").read_text() == "keep me\n"
    assert "notes.txt" in git.git(str(repo), "status", "--porcelain").stdout  # untouched, uncommitted
    assert repo_memory_read(fake_task_input(repoPath=str(repo))).output_data["initializedRepo"] is False


def _workflow(name):
    import json
    from pathlib import Path
    return json.loads((Path(__file__).resolve().parents[1] / "workflows" / f"{name}.json").read_text())


def test_a_greenfield_request_is_built_not_just_labelled():
    """Run 9e3b7a4b: the front door classified an empty repo as greenfield, said project_bootstrap handles
    that, and stopped without starting it."""
    switch = next(t for t in _workflow("sdlc_intake")["tasks"] if t["taskReferenceName"] == "route_switch")
    run, end = switch["decisionCases"]["greenfield"]
    assert (run["type"], run["subWorkflowParam"]["name"]) == ("SUB_WORKFLOW", "project_bootstrap")
    assert run["inputParameters"]["firstFeature"] == "${workflow.variables.routeDecision.instruction}"
    assert run["inputParameters"]["branchName"] == "${workflow.input.branchName}"
    output = end["inputParameters"]["workflowOutput"]
    assert output["sourceHandoff"] == "${run_bootstrap.output.sourceHandoff}"
    assert output["delivered"] == "${run_bootstrap.output.bootstrapped}"


def test_a_new_project_is_built_on_its_own_branch_and_a_failing_skeleton_is_repaired():
    workflow = _workflow("project_bootstrap")
    by_ref = {}

    def walk(tasks):
        for task in tasks:
            by_ref[task["taskReferenceName"]] = task
            for branch in (task.get("decisionCases") or {}).values():
                walk(branch)
            walk(task.get("defaultCase") or [])
    walk(workflow["tasks"])
    worktree = "${workspace.output.worktreePath}"
    assert by_ref["scaffold"]["inputParameters"]["worktreePath"] == worktree
    assert by_ref["skeleton_commit"]["inputParameters"]["repoPath"] == worktree
    assert by_ref["verify"]["subWorkflowParam"]["name"] == "test_cycle"
    assert by_ref["verify"]["inputParameters"]["testCommands"] == "${workflow.input.verificationCommand}"
    assert "tests_passed" in by_ref["skeleton_gate"]["decisionCases"]
    assert by_ref["run_feature"]["inputParameters"]["workspacePath"] == worktree
    final = by_ref["feature_verify"]["inputParameters"]
    assert by_ref["feature_verify"]["subWorkflowParam"]["name"] == "test_cycle"
    assert final["testCommands"] == "${workflow.input.verificationCommand}" and final["testMode"] == "full"
    assert by_ref["outcome"]["inputParameters"]["featureTestsPassed"] == "${feature_verify.output.testsPassed}"
    for end in ("terminate_feature", "terminate_skeleton_ready", "terminate_skeleton_not_green"):
        output = by_ref[end]["inputParameters"]["workflowOutput"]
        assert output["sourceHandoff"]["branch"] == "${workspace.output.branch}"


def test_a_project_is_not_bootstrapped_unless_its_own_tests_pass_on_the_final_commit(fake_task_input):
    """code_parallel's verified stays true when its targeted tests could not run (run 1117790e)."""
    delivered = {"skeletonGreen": True, "featureDelivered": True, "finalHead": "abc"}
    unproven = bootstrap_outcome(fake_task_input(**delivered, featureTestsPassed=False,
                                                 featureTestState="tests_failed")).output_data
    assert unproven["verdict"] == "FEATURE_INCOMPLETE" and unproven["bootstrapped"] is False
    assert "tests_failed" in unproven["report"]
    assert bootstrap_outcome(fake_task_input(**delivered, featureTestsPassed=True)).output_data["verdict"] == "BOOTSTRAPPED"


def test_bug_adopt_removes_the_fork_it_adopted(tmp_path, fake_task_input):
    """worktree_remove already prefixes the worktrees directory; passing it prefixed left every bug_fix
    run's fork registered (review of PR 16)."""
    from common import git
    repo = str(tmp_path / "repo")
    git.ensure_ready(repo)
    with open(os.path.join(repo, "a.py"), "w") as fh:
        fh.write("x = 1\n")
    git.git(repo, "add", "-A")
    git.git(repo, "commit", "-m", "base")
    wt = git.worktree_add(repo, "fix")["worktreePath"]
    with open(os.path.join(wt, "b.py"), "w") as fh:
        fh.write("y = 2\n")
    git.git(wt, "add", "-A")
    git.git(wt, "commit", "-m", "fix")
    out = bug_adopt(fake_task_input(sharedRepo=repo, groupId="fix")).output_data
    assert out["adopted"] is True and os.path.exists(os.path.join(repo, "b.py"))
    assert not os.path.isdir(wt) and git.WORKTREES not in git.git(repo, "worktree", "list", "--porcelain").stdout
