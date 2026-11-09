"""Phase 6: program-scale ranks (G4) and persistent repo_memory with lesson decay (G5/H10).

Covers the pure logic (``common/program.py``, ``common/repo_memory.py``) and the thin workers:
topological ranks + the honest program report, and the read/write/decay of on-branch knowledge where a
lesson whose referenced files are gone is invalidated rather than trusted.
"""
import os

import common  # noqa: F401 -- sys.path shim

from stages.tasks import program_outcome, repo_memory_read, repo_memory_write
from common import git, program, repo_memory


# ─────────────────────────── program ranks + outcome (G4) ──────────────────────
def test_topo_ranks_orders_and_detects_cycles():
    subs = [{"id": "api", "dependsOn": ["core"]}, {"id": "core"},
            {"id": "ui", "dependsOn": ["api", "core"]}]
    out = program.topo_ranks(subs)
    assert out["ranks"] == [["core"], ["api"], ["ui"]] and out["cyclic"] == []
    cyc = program.topo_ranks([{"id": "a", "dependsOn": ["b"]}, {"id": "b", "dependsOn": ["a"]}])
    assert cyc["ranks"] == [] and set(cyc["cyclic"]) == {"a", "b"}


def test_program_report_verdicts():
    subs = [{"id": "core"}, {"id": "api", "dependsOn": ["core"]}]
    good = program.program_report(subsystems=subs, delivered=True, verdict="DELIVERED", total=2,
                                  done_count=2, tests_passed=True, final_head="abc")
    assert good["verdict"] == "PROGRAM_DELIVERED" and good["ranks"] == [["core"], ["api"]]
    bad = program.program_report(subsystems=subs, delivered=False, verdict="INCOMPLETE", total=2,
                                 done_count=1, tests_passed=False)
    assert bad["verdict"] == "PROGRAM_INCOMPLETE"
    cyc = program.program_report(subsystems=[{"id": "a", "dependsOn": ["b"]}, {"id": "b", "dependsOn": ["a"]}],
                                 delivered=False, verdict="x")
    assert cyc["verdict"] == "PROGRAM_PLAN_CYCLIC"


def test_program_outcome_worker_emits_decayable_lessons(fake_task_input):
    out = program_outcome(fake_task_input(
        subsystems=[{"id": "core", "description": "the core", "files": ["core.py"]}],
        delivered=True, verdict="DELIVERED", total=1, doneCount=1, testsPassed=True,
        finalHead="deadbeef")).output_data
    assert out["verdict"] == "PROGRAM_DELIVERED"
    assert out["lessons"][0]["refs"] == ["core.py"] and "core" in out["lessons"][0]["text"]


# ─────────────────────────── repo_memory decay (G5 / H10) ──────────────────────
def test_decay_invalidates_lessons_whose_refs_vanished(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    open(os.path.join(repo, "alive.py"), "w").close()
    mem = repo_memory.merge_lessons(repo_memory._default(), [
        {"text": "alive module", "refs": ["alive.py"], "confidence": "high"},
        {"text": "ghost module", "refs": ["gone.py"], "confidence": "high"},
        {"text": "opinion, no refs", "refs": []},
    ])
    decayed = repo_memory.decay_lessons(mem, repo)
    assert decayed["invalidated"] == 1
    live = {les["text"] for les in repo_memory.live_lessons(decayed["memory"])}
    assert "alive module" in live and "ghost module" not in live and "opinion, no refs" in live
    # The digest a next run sees contains the live lesson, not the stale one.
    dg = repo_memory.digest(decayed["memory"])
    assert "alive module" in dg and "ghost module" not in dg


def test_repo_memory_write_then_read_roundtrip_and_decay(tmp_path, fake_task_input):
    repo = str(tmp_path / "repo")
    git.ensure_ready(repo)
    open(os.path.join(repo, "core.py"), "w").close()
    git.git(repo, "add", "-A"); git.git(repo, "commit", "-m", "base")

    # Run 1 persists a lesson about core.py (refs it).
    w = repo_memory_write(fake_task_input(repoPath=repo, lessons=[
        {"text": "core.py holds the registry", "refs": ["core.py"], "confidence": "high"}])).output_data
    assert w["written"] is True and w["lessons"] == 1
    assert os.path.exists(os.path.join(repo, repo_memory.MEMORY_PATH))

    # A later run reads it -> the lesson is live (plans using prior memory).
    r1 = repo_memory_read(fake_task_input(repoPath=repo)).output_data
    assert r1["liveLessons"] == 1 and "core.py holds the registry" in r1["digest"]

    # The code moves on: core.py is removed. The next read decays the now-stale lesson (H10).
    os.remove(os.path.join(repo, "core.py"))
    r2 = repo_memory_read(fake_task_input(repoPath=repo)).output_data
    assert r2["invalidated"] == 1 and r2["liveLessons"] == 0
    assert "core.py holds the registry" not in r2["digest"]
