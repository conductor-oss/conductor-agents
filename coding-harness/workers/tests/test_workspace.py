from __future__ import annotations

from pathlib import Path

from common import git
from gitops.tasks import workspace_cleanup, workspace_prepare


def test_workspace_prepare_snapshots_dirty_source_and_preserves_checkout(
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

    assert out["ignoredSourceChanges"] == 0
    assert out["baselineIncludedPaths"] == ["tracked.txt", "untracked.txt"]
    assert Path(out["worktreePath"]).joinpath("tracked.txt").read_text() == "local edit\n"
    assert Path(out["worktreePath"]).joinpath("untracked.txt").read_text() == "local only\n"
    assert tracked.read_text() == "local edit\n"
    assert git.head(str(tmp_git_repo)) == head_before
    assert git.git(str(tmp_git_repo), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == branch_before
    assert git.git(str(tmp_git_repo), "status", "--porcelain").stdout


def test_workspace_snapshot_covers_staged_unstaged_deleted_renamed_and_generated_state(
        tmp_git_repo, fake_task_input):
    repo = str(tmp_git_repo)
    (tmp_git_repo / "staged.txt").write_text("old\n")
    (tmp_git_repo / "delete.txt").write_text("delete me\n")
    (tmp_git_repo / "rename.txt").write_text("rename me\n")
    git.git(repo, "add", "staged.txt", "delete.txt", "rename.txt")
    git.git(repo, "commit", "-m", "fixture state")

    (tmp_git_repo / "staged.txt").write_text("staged version\n")
    git.git(repo, "add", "staged.txt")
    (tmp_git_repo / "staged.txt").write_text("final worktree version\n")
    (tmp_git_repo / "delete.txt").unlink()
    git.git(repo, "mv", "rename.txt", "renamed.txt")
    (tmp_git_repo / "build").mkdir()
    (tmp_git_repo / "build" / "visible.cache").write_text("cache\n")
    source_head = git.head(repo)
    source_branch = git._current_branch(repo)
    source_status = git.git(repo, "status", "--porcelain=v1").stdout
    source_cached = git.git(repo, "diff", "--cached", "--binary").stdout
    source_unstaged = git.git(repo, "diff", "--binary").stdout

    out = workspace_prepare(fake_task_input(
        repoPath=repo, workflowId="all-states", branch="feature/all-states")).output_data
    workspace = out["worktreePath"]

    assert out["branch"] == git.run_specific_branch("feature/all-states", "all-states")
    assert set(out["baselineIncludedPaths"]) == {
        "build/visible.cache", "delete.txt", "renamed.txt", "staged.txt"}
    assert Path(workspace, "staged.txt").read_text() == "final worktree version\n"
    assert not Path(workspace, "delete.txt").exists()
    assert Path(workspace, "renamed.txt").read_text() == "rename me\n"
    assert Path(workspace, "build/visible.cache").read_text() == "cache\n"
    assert git.head(repo) == source_head
    assert git._current_branch(repo) == source_branch
    assert git.git(repo, "status", "--porcelain=v1").stdout == source_status
    assert git.git(repo, "diff", "--cached", "--binary").stdout == source_cached
    assert git.git(repo, "diff", "--binary").stdout == source_unstaged


def test_workspace_snapshot_does_not_force_add_truly_ignored_paths(
        tmp_git_repo, fake_task_input):
    repo = str(tmp_git_repo)
    (tmp_git_repo / ".gitignore").write_text("ignored-cache/\n")
    git.git(repo, "add", ".gitignore")
    git.git(repo, "commit", "-m", "ignore fixture")
    (tmp_git_repo / "ignored-cache").mkdir()
    (tmp_git_repo / "ignored-cache" / "blob").write_text("ignored\n")

    out = workspace_prepare(fake_task_input(
        repoPath=repo, workflowId="ignored", branch="feature/ignored")).output_data

    assert out["baselineIncludedPaths"] == []
    assert not Path(out["worktreePath"], "ignored-cache", "blob").exists()
    assert Path(repo, "ignored-cache", "blob").exists()


def test_requested_branch_collision_allocates_new_ref_without_moving_existing_one(
        tmp_git_repo, fake_task_input):
    repo = str(tmp_git_repo)
    original_head = git.head(repo)
    owned = git.run_specific_branch("feature/collision", "collision-run")
    git.git(repo, "branch", owned)

    out = workspace_prepare(fake_task_input(
        repoPath=repo, workflowId="collision-run", branch="feature/collision")).output_data

    assert out["branch"] == f"{owned}-2"
    assert git.git(repo, "rev-parse", owned).stdout.strip() == original_head
    assert git.git(repo, "rev-parse", out["branch"]).stdout.strip() == out["baselineCommit"]


def test_parent_run_id_owns_nested_issue_branch(tmp_git_repo, fake_task_input):
    out = workspace_prepare(fake_task_input(
        repoPath=str(tmp_git_repo), workflowId="child-code-parallel-run",
        branchRunId="cd3840a2-7f6c-4f13-b51c-cc7bd6536cdc",
        branch="harness/issue-132",
    )).output_data

    assert out["branch"] == "harness/issue-132-cd3840a2"


def test_subdirectory_repo_path_still_snapshots_the_entire_checkout(
        tmp_git_repo, fake_task_input):
    repo = str(tmp_git_repo)
    nested = tmp_git_repo / "src" / "nested"
    nested.mkdir(parents=True)
    (tmp_git_repo / "outside.txt").write_text("outside\n")
    (nested / "inside.txt").write_text("inside\n")

    out = workspace_prepare(fake_task_input(
        repoPath=str(nested), workflowId="nested-path", branch="feature/nested")).output_data

    assert out["sourceRepoPath"] == repo
    assert out["baselineIncludedPaths"] == ["outside.txt", "src/nested/inside.txt"]
    assert Path(out["worktreePath"], "outside.txt").read_text() == "outside\n"
    assert Path(out["worktreePath"], "src", "nested", "inside.txt").read_text() == "inside\n"


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


def test_address_pr_initial_and_revision_runs_keep_the_checked_out_pr_branch(
        tmp_git_repo, fake_task_input):
    repo = str(tmp_git_repo)
    git.git(repo, "checkout", "-b", "feature/pr-head")

    initial = workspace_prepare(fake_task_input(
        repoPath=repo, workspacePath=repo, workflowId="address-initial",
        branch="feature/pr-head")).output_data
    revision = workspace_prepare(fake_task_input(
        repoPath=repo, workspacePath=initial["worktreePath"],
        workflowId="address-revision", branch="feature/pr-head")).output_data

    assert initial["worktreePath"] == repo
    assert initial["branch"] == "feature/pr-head"
    assert initial["owned"] is False
    assert revision["worktreePath"] == repo
    assert revision["branch"] == "feature/pr-head"
    assert revision["owned"] is False
