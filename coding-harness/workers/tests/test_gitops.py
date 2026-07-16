"""Unit tests for the local-git gitops worker tasks.

Covers branch/worktree naming + placement and ``merge_worktrees`` against a real
throwaway repo (``tmp_git_repo``). No network, no real ``gh``. The only external
dependency — the Claude Agent SDK conflict-resolver invoked by ``merge_worktrees``
— is mocked at ``common.claude.run_agent`` so no LLM runs.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from common import git
from gitops.tasks import create_branch, merge_worktrees, worktree_add


def _completed(result) -> bool:
    return result.status.value == "COMPLETED"


def _commit_file(repo: str, rel: str, content: str, message: str) -> None:
    (Path(repo) / rel).write_text(content)
    git.git(repo, "add", rel)
    git.git(repo, "commit", "-m", message)


# --- core path 1: branch / worktree naming + placement ----------------------

def test_create_branch_switches_to_named_branch(fake_task_input, tmp_git_repo):
    task = fake_task_input(repoPath=str(tmp_git_repo), name="feature/login")
    result = create_branch(task)
    assert _completed(result)
    assert result.output_data["branch"] == "feature/login"
    # `git checkout -B` actually moved HEAD onto the new branch.
    assert git._current_branch(str(tmp_git_repo)) == "feature/login"


def test_create_branch_is_rerunnable(fake_task_input, tmp_git_repo):
    """`-B` recreates the ref, so re-running the same name must not error."""
    repo = str(tmp_git_repo)
    assert _completed(create_branch(fake_task_input(repoPath=repo, name="wip")))
    second = create_branch(fake_task_input(repoPath=repo, name="wip"))
    assert _completed(second)
    assert second.output_data["branch"] == "wip"


def test_worktree_add_naming_and_placement(fake_task_input, tmp_git_repo):
    task = fake_task_input(repoPath=str(tmp_git_repo), name="alpha")
    result = worktree_add(task)
    assert _completed(result)
    out = result.output_data
    # Branch name is derived via GROUP_BRANCH; dir lands under .cc-worktrees/.
    assert out["branch"] == "cc-group-alpha"
    expected = tmp_git_repo / git.WORKTREES / "alpha"
    assert out["worktreePath"] == str(expected)
    assert (expected / ".git").exists()
    assert len(out["initialCommit"]) >= 7


def test_worktree_add_handles_collision(fake_task_input, tmp_git_repo):
    """A stale worktree/branch of the same name is pruned+removed, so a re-run
    with the same name succeeds rather than failing on 'already exists'."""
    repo = str(tmp_git_repo)
    first = worktree_add(fake_task_input(repoPath=repo, name="dup"))
    second = worktree_add(fake_task_input(repoPath=repo, name="dup"))
    assert _completed(first) and _completed(second)
    assert second.output_data["branch"] == "cc-group-dup"
    assert Path(second.output_data["worktreePath"], ".git").exists()


# --- core path 2: merge_worktrees -------------------------------------------

def test_merge_worktrees_clean_merge(fake_task_input, tmp_git_repo):
    repo = str(tmp_git_repo)
    # A group branch adds a new file; merging it back is conflict-free.
    git.git(repo, "checkout", "-b", "cc-group-a")
    _commit_file(repo, "feature.txt", "hello\n", "add feature")
    git.git(repo, "checkout", "main")

    result = merge_worktrees(fake_task_input(repoPath=repo, groupIds="a"))
    assert _completed(result)
    out = result.output_data
    assert out["merged"] == ["cc-group-a"]
    assert out["conflicts"] == []
    assert out["resolved"] == []
    # The merge actually landed the branch's file on the change branch.
    assert (tmp_git_repo / "feature.txt").exists()


def test_merge_worktrees_aggregates_multiple_branches(fake_task_input, tmp_git_repo):
    repo = str(tmp_git_repo)
    for gid, fname in (("g1", "one.txt"), ("g2", "two.txt")):
        git.git(repo, "checkout", "main")
        git.git(repo, "checkout", "-b", f"cc-group-{gid}")
        _commit_file(repo, fname, "x\n", f"add {fname}")
    git.git(repo, "checkout", "main")

    # groupIds accepts a comma-separated string (Conductor passes strings).
    result = merge_worktrees(fake_task_input(repoPath=repo, groupIds="g1, g2"))
    assert _completed(result)
    out = result.output_data
    assert out["merged"] == ["cc-group-g1", "cc-group-g2"]
    assert out["conflicts"] == []
    assert (tmp_git_repo / "one.txt").exists()
    assert (tmp_git_repo / "two.txt").exists()


def _make_conflict(repo: str, gid: str) -> None:
    """Diverge README on cc-group-<gid> and on main so a merge conflicts."""
    git.git(repo, "checkout", "-b", f"cc-group-{gid}")
    _commit_file(repo, "README.md", "group side\n", "group edit")
    git.git(repo, "checkout", "main")
    _commit_file(repo, "README.md", "main side\n", "main edit")


def test_merge_worktrees_surfaces_and_resolves_conflict(
    fake_task_input, tmp_git_repo, monkeypatch
):
    repo = str(tmp_git_repo)
    _make_conflict(repo, "b")

    seen = {}

    def fake_run_agent(prompt, *, cwd, model=None, write=False, timeout=None, **kw):
        # Stand in for the SDK resolver: clear markers by taking our side.
        seen["prompt"] = prompt
        seen["cwd"] = cwd
        for f in git.has_conflicts(cwd):
            git.git(cwd, "checkout", "--ours", "--", f, check=False)
        return {"ok": True, "tokens": 42, "cost_usd": 0.01}

    monkeypatch.setattr("common.claude.run_agent", fake_run_agent)

    result = merge_worktrees(fake_task_input(repoPath=repo, groupIds="b"))
    assert _completed(result)
    out = result.output_data
    # The conflict is surfaced (not swallowed) AND recorded as resolved.
    assert out["conflicts"] == ["cc-group-b"]
    assert out["resolved"] == ["cc-group-b"]
    assert out["tokenUsed"] == 42
    assert out["costUsd"] == 0.01
    # Tree is left clean — no lingering conflict markers.
    assert git.has_conflicts(repo) == []
    # The resolver was pointed at the repo and told which file conflicted.
    assert seen["cwd"] == repo
    assert "README.md" in seen["prompt"]


def test_merge_worktrees_aborts_when_resolution_fails(
    fake_task_input, tmp_git_repo, monkeypatch
):
    repo = str(tmp_git_repo)
    _make_conflict(repo, "c")

    def fake_run_agent(prompt, *, cwd, model=None, write=False, timeout=None, **kw):
        return {"ok": False, "error": "could not resolve", "tokens": 5, "cost_usd": 0.0}

    monkeypatch.setattr("common.claude.run_agent", fake_run_agent)

    result = merge_worktrees(fake_task_input(repoPath=repo, groupIds="c"))
    # Fail-soft: task COMPLETES but reports the unresolved conflict...
    assert _completed(result)
    out = result.output_data
    assert out["conflicts"] == ["cc-group-c"]
    assert out["resolved"] == []
    # ...and the merge was aborted, so the working tree is NOT left broken.
    assert git.has_conflicts(repo) == []


# --- env-configurable git values --------------------------------------------

def test_worktree_add_copies_default_paths(fake_task_input, tmp_git_repo):
    """With no override, the default test/ dir + package.json are copied into the
    fresh worktree (they live only in the main repo, not on the group branch)."""
    repo = str(tmp_git_repo)
    (tmp_git_repo / "test").mkdir()
    (tmp_git_repo / "test" / "spec.txt").write_text("t\n")
    (tmp_git_repo / "package.json").write_text("{}\n")

    result = worktree_add(fake_task_input(repoPath=repo, name="wd"))
    assert _completed(result)
    wt = Path(result.output_data["worktreePath"])
    assert (wt / "test" / "spec.txt").exists()
    assert (wt / "package.json").exists()


def test_worktree_copy_paths_env_override(fake_task_input, tmp_git_repo, monkeypatch):
    """WORKTREE_COPY_PATHS changes which paths are copied: reloading common.git
    picks up the env override, and only the listed paths land in the worktree."""
    monkeypatch.setenv("WORKTREE_COPY_PATHS", "extra.txt, , nested")
    importlib.reload(git)
    try:
        assert git.WORKTREE_COPY_PATHS == ["extra.txt", "nested"]  # blanks dropped
        (tmp_git_repo / "extra.txt").write_text("e\n")
        (tmp_git_repo / "nested").mkdir()
        (tmp_git_repo / "nested" / "f.txt").write_text("n\n")
        # A default path that is NOT in the override must not be copied.
        (tmp_git_repo / "package.json").write_text("{}\n")

        result = worktree_add(fake_task_input(repoPath=str(tmp_git_repo), name="ov"))
        assert _completed(result)
        wt = Path(result.output_data["worktreePath"])
        assert (wt / "extra.txt").exists()
        assert (wt / "nested" / "f.txt").exists()
        assert not (wt / "package.json").exists()
    finally:
        monkeypatch.delenv("WORKTREE_COPY_PATHS", raising=False)
        importlib.reload(git)


def test_git_identity_defaults_from_env(monkeypatch):
    """GIT_IDENTITY_NAME/GIT_IDENTITY_EMAIL feed the module-level identity defaults
    (reload picks up the env), while the built-in defaults are otherwise preserved."""
    # Baseline defaults with no env set.
    monkeypatch.delenv("GIT_IDENTITY_NAME", raising=False)
    monkeypatch.delenv("GIT_IDENTITY_EMAIL", raising=False)
    importlib.reload(git)
    try:
        assert git.GIT_IDENTITY_NAME == "conductor-code"
        assert git.GIT_IDENTITY_EMAIL == "harness@conductor.local"

        monkeypatch.setenv("GIT_IDENTITY_NAME", "ci-bot")
        monkeypatch.setenv("GIT_IDENTITY_EMAIL", "ci@example.com")
        importlib.reload(git)
        assert git.GIT_IDENTITY_NAME == "ci-bot"
        assert git.GIT_IDENTITY_EMAIL == "ci@example.com"
    finally:
        monkeypatch.delenv("GIT_IDENTITY_NAME", raising=False)
        monkeypatch.delenv("GIT_IDENTITY_EMAIL", raising=False)
        importlib.reload(git)
