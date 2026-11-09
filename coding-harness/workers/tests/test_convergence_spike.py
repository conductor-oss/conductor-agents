"""Group 9 + gap closure: the convergence_spike worker glue over the Phase 1 primitives.

The pure primitives (contract_freeze, file_ownership, continuous_integrate, budget_gate) have their own
tests; this covers the three workers that wire them into the wave-looped workflow:

  * ownership-GATED dispatch — a wave dispatches only items whose shared files are free, and blocked
    items are deferred to the next wave (Gap 1: ownership now gates dispatch, not just records);
  * wave finalization — the integrator releases the wave's ownership and records each agent's real
    spend on the board so budget_gate_check can halt the next wave (Gap 2: spend is now live);
  * the honest GO/NO-GO fold read from the durable board (reopen facts) + the final pending set.
"""
import os

import common  # noqa: F401 -- sys.path shim for swarm_substrate

from campaign.tasks import (convergence_build_forks, convergence_integrate,
                            convergence_outcome)
from common import budget_gate, file_ownership, git
from swarm_substrate import blackboard as bb


def _isolate_board(monkeypatch, tmp_path):
    """Point the workers' default blackboard at a tmp dir (they resolve it from SWARM_BLACKBOARD_DIR)."""
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))


_OVERLAP = [
    {"id": "alpha", "description": "add alpha", "files": ["registry.py", "alpha.py"]},
    {"id": "beta", "description": "add beta", "files": ["registry.py", "beta.py"]},
    {"id": "gamma", "description": "add gamma", "files": ["registry.py", "gamma.py"]},
]


# ─────────────────────────── build_forks: ungated shape (no board) ────────────
def test_build_forks_shape_without_board(fake_task_input):
    out = convergence_build_forks(fake_task_input(
        repoPath="/tmp/shared", subtasks=_OVERLAP, contractPaths=["contract.py"])).output_data
    # No correlationId => no ownership gating; all three dispatch.
    assert out["forkCount"] == 3 and out["remaining"] == []
    assert out["groupIds"] == "alpha,beta,gamma"
    assert [t["name"] for t in out["dynamicTasks"]] == ["code_subtask"] * 3
    beta = out["dynamicTasksInput"]["beta"]
    assert beta["repoPath"] == "/tmp/shared" and beta["allowedWriteRoots"] == ["registry.py", "beta.py"]
    assert "contract.py" in beta["prompt"] and "SHARED" in beta["prompt"]
    assert "version" not in out["dynamicTasks"][0]["subWorkflowParam"]


# ─────────────────────────── Gap 1: ownership GATES dispatch ──────────────────
def test_ownership_gates_dispatch_across_waves(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-gate"
    # Wave 1: all three contend on registry.py -> only the first claimant dispatches.
    w1 = convergence_build_forks(fake_task_input(
        repoPath="/tmp/shared", correlationId=cid, subtasks=_OVERLAP,
        contractPaths=["contract.py"])).output_data
    assert w1["groupIds"] == "alpha"
    assert w1["forkCount"] == 1 and w1["remainingCount"] == 2
    assert [r["id"] for r in w1["remaining"]] == ["beta", "gamma"]
    assert file_ownership.owner_map(cid)["registry.py"] == "alpha"

    # Release alpha's claims (what convergence_integrate does after the wave).
    for path in ["registry.py", "alpha.py"]:
        file_ownership.release(cid, "alpha", path)

    # Wave 2: beta now wins registry.py; gamma still blocked.
    w2 = convergence_build_forks(fake_task_input(
        repoPath="/tmp/shared", correlationId=cid, subtasks=w1["remaining"],
        contractPaths=["contract.py"])).output_data
    assert w2["groupIds"] == "beta" and [r["id"] for r in w2["remaining"]] == ["gamma"]


# ─────────────────────── integrate: release + spend recording (Gap 2) ─────────
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


def test_integrate_releases_ownership_and_records_spend(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-wave"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "registry.py"), "w") as fh:
        fh.write("H = {}\n")
    _commit_all(shared, "base")
    shared_branch = git._current_branch(shared)

    file_ownership.claim(cid, "alpha", "registry.py")
    _fork_worktree(shared, "alpha", shared_branch, edit=("registry.py", "H = {'a': 1}\n"))

    out = convergence_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=shared_branch, correlationId=cid,
        contractPaths=["contract.py"], groupIds="alpha",
        dispatched=[{"id": "alpha", "files": ["registry.py"], "attempt": 0}],
        joined={"alpha": {"output": {"costUsd": 0.42, "tokenUsed": 1234, "model": "claude-x"}}},
    )).output_data

    assert [i["item"] for i in out["integrated"]] == ["alpha"]
    assert out["big_bang_merge"] is False and out["retry"] == []
    # Ownership released => registry.py is free again for the next wave.
    assert out["releasedFiles"] == 1 and file_ownership.owner_map(cid) == {}
    # Spend recorded on the board => budget_gate now sees real spend (not $0).
    assert out["spendRecorded"] == 1
    spend = budget_gate.read_spend(cid)
    assert spend["usd"] == 0.42 and spend["by_author"]["alpha"]["usd"] == 0.42


def test_integrate_requeues_empty_delivery_then_terminates_at_cap(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-empty"
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    with open(os.path.join(shared, "registry.py"), "w") as fh:
        fh.write("H = {}\n")
    _commit_all(shared, "base")
    shared_branch = git._current_branch(shared)
    # A fork worktree that made NO commit (agent delivered nothing / failed).
    _fork_worktree(shared, "alpha", shared_branch)  # no edit -> branch == head

    item = {"id": "alpha", "description": "add alpha", "files": ["registry.py", "alpha.py"], "attempt": 0}
    out = convergence_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=shared_branch, correlationId=cid,
        contractPaths=["contract.py"], groupIds="alpha", dispatched=[item], remaining=[],
        maxAttempts=2, joined={})).output_data
    # Empty delivery is NOT counted as integrated; it is re-queued for the next wave.
    assert out["integrated"] == [] and out["reopened"] == []
    assert len(out["retry"]) == 1 and out["retry"][0]["attempt"] == 1
    assert out["nextPending"] == out["retry"]
    # A terminal reopen fact must NOT exist yet (still retrying).
    assert bb.read_facts(cid, kind="reopen") == []

    # Second attempt also empty, now at the cap (attempt+1 == maxAttempts) => terminal reopen.
    item2 = {**item, "attempt": 1}
    out2 = convergence_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=shared_branch, correlationId=cid,
        contractPaths=["contract.py"], groupIds="alpha", dispatched=[item2], remaining=[],
        maxAttempts=2, joined={})).output_data
    assert out2["retry"] == [] and out2["nextPending"] == []
    assert len(out2["reopened"]) == 1 and out2["reopened"][0]["reason"] == "empty_delivery"
    assert len(bb.read_facts(cid, kind="reopen")) == 1


def test_integrate_routes_contract_edit_as_controlled_event(tmp_path, fake_task_input):
    shared = str(tmp_path / "shared")
    git.ensure_ready(shared)
    for name, text in [("contract.py", "def handle(x):\n    return x\n"), ("registry.py", "H = {}\n")]:
        with open(os.path.join(shared, name), "w") as fh:
            fh.write(text)
    _commit_all(shared, "base")
    shared_branch = git._current_branch(shared)
    for gid, path, text in [("ok", "registry.py", "H = {'a': 1}\n"),
                            ("bad", "contract.py", "def handle(x):\n    return x + 1\n")]:
        wt = os.path.join(shared, git.WORKTREES, gid)
        git.git(shared, "worktree", "add", wt, "-b", git.group_branch(shared, gid), shared_branch)
        with open(os.path.join(wt, path), "w") as fh:
            fh.write(text)
        _commit_all(wt, f"{gid} change")

    out = convergence_integrate(fake_task_input(
        sharedRepo=shared, sharedBranch=shared_branch,
        contractPaths=["contract.py"], groupIds="ok,bad")).output_data

    assert [i["item"] for i in out["integrated"]] == ["ok"]
    assert len(out["controlledEvents"]) == 1
    ce = out["controlledEvents"][0]
    assert ce["item"] == "bad" and ce["reason"] == "contract_controlled_event"
    assert "contract.py" in ce["conflicts"]


# ─────────────────────────── outcome: honest GO/NO-GO from the board ──────────
def test_outcome_go_when_all_integrated_green_and_nothing_left(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    out = convergence_outcome(fake_task_input(
        correlationId="run-go", subtasks=_OVERLAP, pendingRemaining=[],
        testCycleState="tests_passed", testsPassed=True, finalHead="abc123def456")).output_data
    assert out["converged"] is True and out["verdict"] == "GO"
    assert out["integratedCount"] == 3 and out["reopenedCount"] == 0 and out["undispatchedCount"] == 0
    assert "3/3" in out["report"]


def test_outcome_nogo_on_budget_halt_names_undispatched(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    out = convergence_outcome(fake_task_input(
        correlationId="run-halt", subtasks=_OVERLAP,
        pendingRemaining=[{"id": "beta"}, {"id": "gamma"}],
        testCycleState="tests_passed", testsPassed=True, finalHead="deadbeef")).output_data
    assert out["converged"] is False and out["verdict"] == "NO-GO"
    assert out["undispatchedCount"] == 2 and out["integratedCount"] == 1
    assert "unresolved" in out["report"] and "budget halt" in out["report"]
    assert "beta" in out["report"] and "gamma" in out["report"]


def test_outcome_nogo_on_reopen_names_mechanism(monkeypatch, tmp_path, fake_task_input):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-reopen"
    bb.post_fact(cid, "beta", "reopen", {
        "op": "reopen", "item": "beta", "reason": "rebase_conflict",
        "mechanism": "continuous rebase of item beta onto main left conflict in: registry.py"})
    out = convergence_outcome(fake_task_input(
        correlationId=cid, subtasks=_OVERLAP, pendingRemaining=[],
        testCycleState="tests_failed_fix_unavailable", testsPassed=False,
        finalHead="cafe")).output_data
    assert out["converged"] is False and out["verdict"] == "NO-GO"
    assert out["reopenedCount"] == 1 and out["integratedCount"] == 2
    assert "registry.py" in out["report"] and "not green" in out["report"]
