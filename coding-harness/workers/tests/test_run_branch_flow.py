"""After a run: its branch is the result. A follow-up run continues on it, a finished run lets go of
its checkout, and the branch is merged locally only when that is safe."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common import git

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


def _commit(repo, rel, content, msg):
    (Path(repo) / rel).write_text(content)
    git.git(str(repo), "add", rel)
    git.git(str(repo), "commit", "-m", msg)


def _run_branch(repo, name="swarm/pg-abc", run="abc"):
    wt = Path(repo) / git.WORKTREES / f"run-{run}"
    git.git(str(repo), "worktree", "add", "-b", name, str(wt))
    _commit(wt, "db.py", "db\n", "work")
    return wt


def test_branch_slug_reads_as_the_request():
    assert git.branch_slug("Build a Postgres-compatible database in Rust") == "swarm/postgres-compatible-database-rust"
    assert git.branch_slug("") == "swarm/run"
    assert git.branch_slug("x" * 200).startswith("swarm/") and len(git.branch_slug("x" * 200)) <= 54
    # The run id is still appended, so two runs asked the same thing never share a branch.
    assert git.run_specific_branch(git.branch_slug("add docs"), "037e1cbe-b243") != git.run_specific_branch(
        git.branch_slug("add docs"), "7348a728-0000")


def test_continuation_checkout_takes_the_branch_from_a_finished_runs_worktree(tmp_git_repo):
    wt = _run_branch(tmp_git_repo)
    path = git.continuation_checkout(str(tmp_git_repo), "swarm/pg-abc")
    assert not wt.exists() and Path(path, "db.py").exists()
    assert Path(path).name == "continue-swarm-pg-abc"
    # Asking again reuses the checkout that holds the branch.
    assert git.continuation_checkout(str(tmp_git_repo), "swarm/pg-abc") == path
    # A branch the user has checked out in their own checkout is continued there, untouched.
    assert git.continuation_checkout(str(tmp_git_repo), "main") == str(Path(tmp_git_repo).resolve())
    with pytest.raises(ValueError, match="does not exist"):
        git.continuation_checkout(str(tmp_git_repo), "nope")


def test_continuation_never_discards_uncommitted_work_in_a_runs_worktree(tmp_git_repo):
    wt = _run_branch(tmp_git_repo)
    (wt / "scratch.py").write_text("unsaved\n")
    with pytest.raises(ValueError, match="uncommitted changes"):
        git.continuation_checkout(str(tmp_git_repo), "swarm/pg-abc")
    assert (wt / "scratch.py").exists()


def test_branch_summary_and_safe_merge(tmp_git_repo):
    repo = str(tmp_git_repo)
    _run_branch(tmp_git_repo)
    s = git.branch_summary(repo, "swarm/pg-abc", "main")
    assert (s["commits"], s["files"], s["insertions"], s["merged"]) == (1, 1, 1, False)
    assert git.branch_summary(repo, "nope", "main") == {"branch": "nope", "exists": False}
    out = git.merge_branch(repo, "swarm/pg-abc", "main")
    assert out["merged"] and out["fastForward"] and Path(repo, "db.py").exists()
    assert git.merge_branch(repo, "swarm/pg-abc", "main")["already"] is True
    assert git.branch_summary(repo, "swarm/pg-abc", "main")["merged"] is True


def test_merge_conflict_is_aborted_and_reported(tmp_git_repo):
    repo = str(tmp_git_repo)
    wt = _run_branch(tmp_git_repo)
    _commit(wt, "README.md", "run side\n", "run edits readme")
    _commit(repo, "README.md", "main side\n", "main edits readme")
    before = git.git(repo, "rev-parse", "HEAD").stdout.strip()
    with pytest.raises(ValueError, match="conflicts; nothing was changed"):
        git.merge_branch(repo, "swarm/pg-abc", "main")
    assert git.git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert not git.git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()


def test_merge_into_a_branch_nobody_has_checked_out_only_fast_forwards(tmp_git_repo):
    repo = str(tmp_git_repo)
    git.git(repo, "branch", "release")
    git.git(repo, "checkout", "-b", "elsewhere")
    _run_branch(tmp_git_repo)
    out = git.merge_branch(repo, "swarm/pg-abc", "release")
    assert out["fastForward"] and git.branch_summary(repo, "swarm/pg-abc", "release")["merged"]
    git.git(repo, "branch", "-f", "release", "main")
    _commit(repo, "other.txt", "x\n", "elsewhere moves")
    git.git(repo, "branch", "-f", "release", "elsewhere")
    with pytest.raises(ValueError, match="has moved"):
        git.merge_branch(repo, "swarm/pg-abc", "release")


def test_remote_info_names_a_github_remote_and_its_default_branch(tmp_git_repo):
    repo = str(tmp_git_repo)
    assert git.remote_info(repo) is None
    git.git(repo, "remote", "add", "origin", "git@github.com:acme/pg.git")
    git.git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert git.remote_info(repo) == {"name": "origin", "url": "git@github.com:acme/pg.git",
                                     "defaultBranch": "main", "github": "acme/pg"}



def test_ship_run_pushes_then_opens_the_pull_request_through_pr_create():
    w = json.loads((WORKFLOWS / "ship_run.json").read_text())
    assert w["version"] == 1 and w["tasks"][0]["taskReferenceName"] == "model_policy"
    assert [t["name"] for t in w["tasks"][1:]] == ["git_push", "pr_create"]
