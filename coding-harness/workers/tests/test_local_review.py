from __future__ import annotations

import subprocess

from common import git
from gitops.tasks import local_diff


def _remote(repo, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git.git(str(repo), "remote", "add", "origin", str(remote))
    git.git(str(repo), "push", "-u", "origin", "main")


def test_local_diff_includes_dirty_and_untracked_files_without_touching_checkout(
        tmp_git_repo, tmp_path, fake_task_input):
    _remote(tmp_git_repo, tmp_path)
    head_before = git.head(str(tmp_git_repo))
    (tmp_git_repo / "README.md").write_text("# changed locally\n")
    (tmp_git_repo / "new.py").write_text("print('local')\n")

    result = local_diff(fake_task_input(
        repoPath=str(tmp_git_repo), baseRemote="origin", baseBranch="main"))
    assert str(result.status.value) == "COMPLETED"
    out = result.output_data
    assert out["baseRef"] == "origin/main"
    assert out["baseCommit"] == head_before
    assert out["headCommit"] == head_before
    assert out["changedFiles"] == ["README.md", "new.py"]
    assert out["untrackedFiles"] == ["new.py"]
    assert "changed locally" in out["diff"]
    assert "new.py" in out["diff"]
    assert (tmp_git_repo / "README.md").read_text() == "# changed locally\n"
    assert (tmp_git_repo / "new.py").exists()
    assert git.head(str(tmp_git_repo)) == head_before


def test_local_diff_rejects_unknown_remote(tmp_git_repo, fake_task_input):
    result = local_diff(fake_task_input(
        repoPath=str(tmp_git_repo), baseRemote="missing", baseBranch="main"))
    assert str(result.status.value) == "FAILED"
    assert "not configured" in result.reason_for_incompletion
