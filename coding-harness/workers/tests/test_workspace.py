from __future__ import annotations

from pathlib import Path

from common import git
from gitops.tasks import workspace_cleanup, workspace_prepare


def test_workspace_prepare_ignores_dirty_source_and_preserves_checkout(
        tmp_git_repo, fake_task_input):
    tracked = tmp_git_repo / "tracked.txt"
    tracked.write_text("committed\n")
    git.git(str(tmp_git_repo), "add", "tracked.txt")
    git.git(str(tmp_git_repo), "commit", "-m", "add tracked")
    branch_before = git.git(
        str(tmp_git_repo), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    head_before = git.head(str(tmp_git_repo))

    tracked.write_text("local edit\n")
    (tmp_git_repo / "untracked.txt").write_text("local only\n")
    result = workspace_prepare(fake_task_input(
        repoPath=str(tmp_git_repo), workflowId="wf-local", branch="feature/local"))
    out = result.output_data

    assert out["ignoredSourceChanges"] == 2
    assert Path(out["worktreePath"]).joinpath("tracked.txt").read_text() == "committed\n"
    assert not Path(out["worktreePath"]).joinpath("untracked.txt").exists()
    assert tracked.read_text() == "local edit\n"
    assert git.head(str(tmp_git_repo)) == head_before
    assert git.git(str(tmp_git_repo), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == branch_before


def test_workspace_resume_and_cleanup_remove_nested_worktrees_but_keep_branches(
        tmp_git_repo, fake_task_input):
    first = workspace_prepare(fake_task_input(
        repoPath=str(tmp_git_repo), workflowId="wf-resume", branch="feature/resume"))
    out = first.output_data
    nested = git.worktree_add(out["worktreePath"], "nested", preserve_existing=True)
    second = workspace_prepare(fake_task_input(
        repoPath=str(tmp_git_repo), workflowId="wf-resume", branch="feature/resume"))
    assert second.output_data["resumed"] is True
    assert second.output_data["worktreePath"] == out["worktreePath"]

    cleaned = workspace_cleanup(fake_task_input(
        sourceRepoPath=out["sourceRepoPath"], worktreePath=out["worktreePath"],
        branch=out["branch"], owned=True, keepWorktree=False, outcome="completed"))
    assert cleaned.output_data["removed"] is True
    assert not Path(out["worktreePath"]).exists()
    assert not Path(nested["worktreePath"]).exists()
    assert git.git(str(tmp_git_repo), "show-ref", "--verify",
                   f"refs/heads/{out['branch']}", check=False).code == 0


def test_inherited_workspace_is_never_cleaned(tmp_git_repo, fake_task_input):
    prepared = workspace_prepare(fake_task_input(
        repoPath=str(tmp_git_repo), workflowId="parent", branch="feature/parent")).output_data
    inherited = workspace_prepare(fake_task_input(
        repoPath=str(tmp_git_repo), workspacePath=prepared["worktreePath"],
        workflowId="child", branch="ignored")).output_data
    assert inherited["owned"] is False

    cleaned = workspace_cleanup(fake_task_input(
        sourceRepoPath=inherited["sourceRepoPath"], worktreePath=inherited["worktreePath"],
        branch=inherited["branch"], owned=False, keepWorktree=False, outcome="completed"))
    assert cleaned.output_data["retained"] is True
    assert Path(inherited["worktreePath"]).is_dir()
