"""Unit tests for the local-git gitops worker tasks.

Covers branch/worktree naming + placement and ``merge_worktrees`` against a real
throwaway repo (``tmp_git_repo``). No network, no real ``gh``. The only external
dependency — the Claude Agent SDK conflict-resolver invoked by ``merge_worktrees``
— is mocked at ``common.claude.run_agent`` so no LLM runs.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from common import git
from gitops.tasks import (
    create_branch,
    git_push,
    inplace_guard,
    merge_worktrees,
    plan_source_detect,
    pr_create,
    prepare_repo,
    reconcile_branch_drift,
    worktree_add,
)


def _completed(result) -> bool:
    return result.status.value == "COMPLETED"


def _commit_file(repo: str, rel: str, content: str, message: str) -> None:
    (Path(repo) / rel).write_text(content)
    git.git(repo, "add", rel)
    git.git(repo, "commit", "-m", message)


def test_prepare_repo_rejects_url_shaped_local_path(fake_task_input, tmp_path: Path):
    malformed = tmp_path / "https:" / "github.com" / "example" / "repo"
    result = prepare_repo(fake_task_input(repoPath=str(malformed)))
    assert not _completed(result)
    assert "local filesystem path" in result.reason_for_incompletion
    assert not malformed.exists()


def test_plan_source_detect_uses_openspec_only_when_present(fake_task_input, tmp_path: Path):
    generic = plan_source_detect(fake_task_input(repoPath=str(tmp_path)))
    assert _completed(generic)
    assert generic.output_data["mode"] == "documents"

    (tmp_path / "openspec").mkdir()
    openspec = plan_source_detect(fake_task_input(repoPath=str(tmp_path)))
    assert _completed(openspec)
    assert openspec.output_data["mode"] == "openspec"


def test_inplace_guard_canonicalizes_abbreviated_commit(fake_task_input, tmp_git_repo):
    repo = str(tmp_git_repo)
    full_head = git.head(repo)
    abbreviated_head = git.git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

    result = inplace_guard(fake_task_input(
        repoPath=repo,
        branch=git._current_branch(repo),
        expectedHead=abbreviated_head,
    ))

    assert _completed(result)
    assert result.output_data["matched"] is True
    assert result.output_data["head"] == full_head


def test_inplace_guard_still_rejects_real_head_drift(fake_task_input, tmp_git_repo):
    repo = str(tmp_git_repo)
    expected = git.git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    _commit_file(repo, "drift.txt", "drift\n", "move head")

    result = inplace_guard(fake_task_input(
        repoPath=repo,
        branch=git._current_branch(repo),
        expectedHead=expected,
    ))
    assert not _completed(result)
    assert "checkout drifted" in result.reason_for_incompletion


def test_git_push_returns_retained_branch_for_nonretryable_permission_rejection(
    fake_task_input, tmp_git_repo, monkeypatch
):
    repo = str(tmp_git_repo)
    monkeypatch.setattr("gitops.tasks.github.ensure_git_auth", lambda: None)
    monkeypatch.setattr("gitops.tasks.git.push", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("refusing to allow a Personal Access Token to create or update workflow "
                     "without `workflow` scope")
    ))
    result = git_push(fake_task_input(repoPath=repo, branch="harness/change"))
    assert _completed(result)
    assert result.output_data["pushed"] is False
    assert result.output_data["publicationState"] == "permission_blocked"
    assert result.output_data["retryable"] is False
    assert result.output_data["head"] == git.head(repo)


def test_git_push_keeps_transient_failures_retryable_as_task_failures(
    fake_task_input, tmp_git_repo, monkeypatch
):
    monkeypatch.setattr("gitops.tasks.github.ensure_git_auth", lambda: None)
    monkeypatch.setattr("gitops.tasks.git.push", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("connection reset by peer")
    ))
    result = git_push(fake_task_input(repoPath=str(tmp_git_repo), branch="harness/change"))
    assert not _completed(result)


def test_pr_create_reuses_existing_pr(fake_task_input, tmp_git_repo, monkeypatch):
    monkeypatch.setattr("gitops.tasks.github.pr_create", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("a pull request for branch already exists: "
                     "https://github.com/acme/app/pull/42")
    ))
    updated = {}
    monkeypatch.setattr("gitops.tasks.github.update_pr_body",
                        lambda repo, number, body: updated.update(repo=repo, number=number, body=body))
    monkeypatch.setattr("gitops.tasks.github.pr_set_draft",
                        lambda repo, number, draft: {"number": number, "draft": draft})
    result = pr_create(fake_task_input(repoPath=str(tmp_git_repo), head="harness/change"))
    assert _completed(result)
    assert result.output_data["existing"] is True
    assert result.output_data["number"] == 42
    assert result.output_data["publicationState"] == "published"
    assert updated["body"] == "## Summary\n\nAutomated change."


def test_pr_create_returns_retained_branch_for_permission_rejection(
    fake_task_input, tmp_git_repo, monkeypatch
):
    monkeypatch.setattr("gitops.tasks.github.pr_create", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("write access to repository not granted")
    ))
    result = pr_create(fake_task_input(repoPath=str(tmp_git_repo), head="harness/change"))
    assert _completed(result)
    assert result.output_data["created"] is False
    assert result.output_data["publicationState"] == "permission_blocked"
    assert result.output_data["branch"] == "harness/change"


def test_commit_always_returns_a_canonical_full_object_id(tmp_git_repo):
    repo = str(tmp_git_repo)
    (tmp_git_repo / "candidate.py").write_text("answer = 42\n")

    created = git.commit(repo, "create candidate")
    no_op = git.commit(repo, "nothing else to commit")

    assert created["commit"] == git.head(repo)
    assert len(created["commit"]) == 40
    assert no_op["commit"] == created["commit"]


def test_prepare_repo_initial_commit_excludes_cache_only_files(fake_task_input, tmp_path: Path):
    repo = tmp_path / "new-repo"
    cache = repo / ".gradle-local"
    cache.mkdir(parents=True)
    (cache / "metadata.bin").write_text("not source")

    result = prepare_repo(fake_task_input(repoPath=str(repo)))

    assert _completed(result)
    assert git.git(str(repo), "ls-tree", "-r", "--name-only", "HEAD").stdout.strip() == ""
    assert (cache / "metadata.bin").exists()


def test_prepare_repo_inplace_branches_before_capturing_all_visible_changes(
        fake_task_input, tmp_git_repo):
    repo = str(tmp_git_repo)
    original_head = git.head(repo)
    (tmp_git_repo / "feature.py").write_text("answer = 42\n")
    (tmp_git_repo / ".gradle-local").mkdir()
    (tmp_git_repo / ".gradle-local" / "cache").write_text("visible generated state")
    result = prepare_repo(fake_task_input(repoPath=repo, inPlace=True, workflowId="run-1"))
    assert _completed(result)
    out = result.output_data
    assert out["baselineCreated"] is True
    assert out["branch"] == "conductor/run-run-1"
    assert out["originalBranch"] == "main"
    assert out["originalHead"] == original_head
    assert out["includedPaths"] == [".gradle-local/cache", "feature.py"]
    assert git.status_files(repo) == set()
    assert "conductor-workspace:run-1:baseline" in git.git(
        repo, "log", "-1", "--format=%B").stdout
    assert git.git(repo, "rev-parse", "main").stdout.strip() == original_head
    assert git.git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines() == [
        ".gradle-local/cache", "README.md", "feature.py"]


def test_prepare_repo_inplace_reuses_its_baseline_on_restart(fake_task_input, tmp_git_repo):
    repo = str(tmp_git_repo)
    (tmp_git_repo / "feature.py").write_text("answer = 42\n")
    first = prepare_repo(fake_task_input(repoPath=repo, inPlace=True, workflowId="run-2"))
    second = prepare_repo(fake_task_input(repoPath=repo, inPlace=True, workflowId="run-2"))
    assert _completed(first) and _completed(second)
    assert second.output_data["baselineCommit"] == first.output_data["baselineCommit"]
    assert second.output_data["baselineCreated"] is False
    assert second.output_data["resumed"] is True


def test_prepare_repo_inplace_includes_generated_only_changes(fake_task_input, tmp_git_repo):
    (tmp_git_repo / ".gradle-local").mkdir()
    (tmp_git_repo / ".gradle-local" / "cache").write_text("x")
    result = prepare_repo(fake_task_input(repoPath=str(tmp_git_repo), inPlace=True))
    assert _completed(result)
    assert result.output_data["includedPaths"] == [".gradle-local/cache"]
    assert git.git(str(tmp_git_repo), "show", "HEAD:.gradle-local/cache").stdout == "x"


def test_create_branch_inplace_creates_the_requested_outcome_branch(fake_task_input, tmp_git_repo):
    source_head = git.head(str(tmp_git_repo))
    result = create_branch(fake_task_input(
        repoPath=str(tmp_git_repo), name="code-parallel", inPlace=True,
        branchRunId="12345678-run"))
    assert _completed(result)
    expected = "code-parallel-12345678"
    assert result.output_data == {"branch": expected, "resumed": False, "inPlace": True}
    assert git._current_branch(str(tmp_git_repo)) == expected
    assert git.head(str(tmp_git_repo)) == source_head
    assert git.git(str(tmp_git_repo), "rev-parse", "main").stdout.strip() == source_head


def test_candidate_bound_push_uses_the_verified_sha_and_rejects_a_moved_branch(tmp_git_repo, tmp_path: Path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    repo = str(tmp_git_repo)
    git.git(repo, "remote", "add", "origin", str(remote))
    candidate = git.head(repo)

    pushed = git.push(repo, branch_name="main", remote="origin", expected_head=candidate)

    assert pushed["candidateBound"] is True
    assert pushed["head"] == candidate
    assert git.git(str(remote), "rev-parse", "main").stdout.strip() == candidate
    assert git.git(repo, "rev-parse", "--abbrev-ref", "main@{upstream}").stdout.strip() == "origin/main"

    _commit_file(repo, "later.py", "value = 2\n", "move branch after verification")
    with pytest.raises(ValueError, match="moved after candidate verification"):
        git.push(repo, branch_name="main", remote="origin", expected_head=candidate)


def test_clone_can_use_a_clean_environment(monkeypatch):
    calls = []

    def fake_run(cmd, *, check, env=None, clean_env=False, **_kwargs):
        calls.append({"cmd": cmd, "check": check, "env": env, "clean_env": clean_env})
        return type("Result", (), {"stdout": "", "stderr": "", "code": 0})()

    monkeypatch.setattr("common.git.run", fake_run)
    monkeypatch.setattr("common.git._current_branch", lambda _repo: "main")
    monkeypatch.setattr("common.git.head", lambda _repo: "a" * 40)

    out = git.clone("/source", "/clone", no_local=True, env={"PATH": "/bin"}, clean_env=True)

    assert out["repoPath"] == "/clone"
    assert calls == [{"cmd": ["git", "clone", "--no-local", "/source", "/clone"],
                      "check": True, "env": {"PATH": "/bin"}, "clean_env": True}]


# --- core path 1: branch / worktree naming + placement ----------------------

def test_create_branch_switches_to_named_branch(fake_task_input, tmp_git_repo):
    task = fake_task_input(
        repoPath=str(tmp_git_repo), name="feature/login", branchRunId="12345678-run")
    result = create_branch(task)
    assert _completed(result)
    assert result.output_data["branch"] == "feature/login-12345678"
    # `git checkout -B` actually moved HEAD onto the new branch.
    assert git._current_branch(str(tmp_git_repo)) == "feature/login-12345678"


def test_create_branch_is_rerunnable_without_resetting_an_existing_ref(fake_task_input, tmp_git_repo):
    repo = str(tmp_git_repo)
    inputs = {"repoPath": repo, "name": "wip", "branchRunId": "12345678-run"}
    assert _completed(create_branch(fake_task_input(**inputs)))
    second = create_branch(fake_task_input(**inputs))
    assert _completed(second)
    assert second.output_data["branch"] == "wip-12345678"
    assert second.output_data["resumed"] is True


def test_run_specific_branch_is_stable_per_run_and_unique_between_runs():
    first = git.run_specific_branch("harness/issue-132", "cd3840a2-1111")
    second = git.run_specific_branch("harness/issue-132", "1a1a1ba3-2222")

    assert first == "harness/issue-132-cd3840a2"
    assert second == "harness/issue-132-1a1a1ba3"
    assert first != second
    assert git.run_specific_branch(first, "cd3840a2-1111") == first


def test_create_branch_force_new_never_reuses_the_checked_out_branch(
        fake_task_input, tmp_git_repo):
    repo = str(tmp_git_repo)
    original = git._current_branch(repo)
    result = create_branch(fake_task_input(
        repoPath=repo, name=original, forceNew=True, workflowId="github-demo-run"))

    assert _completed(result)
    assert result.output_data["branch"] != original
    assert result.output_data["resumed"] is False
    assert git._current_branch(repo) == result.output_data["branch"]


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


def test_merge_worktrees_preflight_surfaces_then_resolves_a_conflict(fake_task_input, tmp_git_repo):
    repo = str(tmp_git_repo)
    _make_conflict(repo, "preflight")
    before = git.head(repo)
    result = merge_worktrees(fake_task_input(repoPath=repo, groupIds="preflight", preflight=True))
    assert _completed(result)
    assert git.head(repo) != before
    assert result.output_data["unresolved"] == []
    assert git.has_conflicts(repo) == []


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


def test_merge_worktrees_reports_non_conflict_errors_as_unresolved(
    fake_task_input, tmp_git_repo
):
    """A missing group branch must never be reported as a successful merge."""
    result = merge_worktrees(fake_task_input(repoPath=str(tmp_git_repo), groupIds="missing"))
    assert _completed(result)
    out = result.output_data
    assert out["merged"] == []
    assert out["conflicts"] == []
    assert out["unresolved"] == ["cc-group-missing"]
    assert out["errors"] and out["errors"][0]["branch"] == "cc-group-missing"


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

    def fake_run_agent(prompt, *, cwd, model=None, write=False, **kw):
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
    assert out["unresolved"] == []
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

    def fake_run_agent(prompt, *, cwd, model=None, write=False, **kw):
        return {"ok": False, "error": "could not resolve", "tokens": 5, "cost_usd": 0.0}

    monkeypatch.setattr("common.claude.run_agent", fake_run_agent)

    result = merge_worktrees(fake_task_input(repoPath=repo, groupIds="c"))
    # Fail-soft: task COMPLETES but reports the unresolved conflict...
    assert _completed(result)
    out = result.output_data
    assert out["conflicts"] == ["cc-group-c"]
    assert out["resolved"] == []
    assert out["unresolved"] == ["cc-group-c"]
    # ...and the merge was aborted, so the working tree is NOT left broken.
    assert git.has_conflicts(repo) == []


# --- core path 3: reconcile_branch_drift (publish_verified_pr's branch-drift path) ---

def _clone_with_origin(source: Path, dest: Path) -> str:
    subprocess.run(["git", "clone", str(source), str(dest)], check=True, capture_output=True)
    git.git(str(dest), "config", "user.name", "Conductor Test")
    git.git(str(dest), "config", "user.email", "test@conductor.local")
    git.git(str(dest), "config", "commit.gpgsign", "false")
    return str(dest)


def test_reconcile_branch_drift_merges_cleanly_when_changes_do_not_overlap(fake_task_input, tmp_git_repo, tmp_path):
    origin = str(tmp_git_repo)
    local = _clone_with_origin(tmp_git_repo, tmp_path / "local")
    # The remote drifted (a new commit landed on origin's main after local cloned it)...
    _commit_file(origin, "from-remote.txt", "remote work\n", "remote drift")
    # ...while local independently produced its own verified candidate commit.
    _commit_file(local, "candidate.txt", "candidate work\n", "verified candidate")
    candidate_head = git.head(local)

    result = reconcile_branch_drift(fake_task_input(repoPath=local, branch="main"))

    assert _completed(result)
    out = result.output_data
    assert out["mergeState"] == "merged"
    assert out["commit"] != candidate_head
    assert out["tokenUsed"] == 0
    assert out["costUsd"] == 0.0
    # Both the candidate's own file and the remote's new file are present --
    # nothing was discarded or force-overwritten.
    assert (Path(local) / "candidate.txt").exists()
    assert (Path(local) / "from-remote.txt").exists()
    # The original verified candidate commit is still reachable as an ancestor
    # (a merge, not a rebase) -- its own SHA/content is never rewritten.
    assert git.git(local, "merge-base", "--is-ancestor", candidate_head, "HEAD", check=False).code == 0


def test_reconcile_branch_drift_resolves_a_real_conflict_and_flags_it_for_reverification(
    fake_task_input, tmp_git_repo, tmp_path, monkeypatch
):
    origin = str(tmp_git_repo)
    local = _clone_with_origin(tmp_git_repo, tmp_path / "local")
    # Both sides edit the exact same file -- a genuine conflict.
    _commit_file(origin, "README.md", "remote side\n", "remote edit")
    _commit_file(local, "README.md", "candidate side\n", "candidate edit")

    def fake_run_agent(prompt, *, cwd, model=None, write=False, **kw):
        for f in git.has_conflicts(cwd):
            git.git(cwd, "checkout", "--ours", "--", f, check=False)
        return {"ok": True, "tokens": 17, "cost_usd": 0.02}

    monkeypatch.setattr("common.claude.run_agent", fake_run_agent)

    result = reconcile_branch_drift(fake_task_input(repoPath=local, branch="main"))

    assert _completed(result)
    out = result.output_data
    # "resolved", not "merged": a real conflict was resolved, so the caller
    # must re-verify this new content before publishing it -- unlike a clean
    # merge, this is genuinely unverified.
    assert out["mergeState"] == "resolved"
    assert out["commit"]
    assert out["tokenUsed"] == 17
    assert out["costUsd"] == 0.02
    assert git.has_conflicts(local) == []


def test_reconcile_branch_drift_leaves_the_tree_clean_when_resolution_fails(
    fake_task_input, tmp_git_repo, tmp_path, monkeypatch
):
    origin = str(tmp_git_repo)
    local = _clone_with_origin(tmp_git_repo, tmp_path / "local")
    _commit_file(origin, "README.md", "remote side\n", "remote edit")
    _commit_file(local, "README.md", "candidate side\n", "candidate edit")
    candidate_head = git.head(local)

    def fake_run_agent(prompt, *, cwd, model=None, write=False, **kw):
        return {"ok": False, "error": "could not resolve", "tokens": 3, "cost_usd": 0.0}

    monkeypatch.setattr("common.claude.run_agent", fake_run_agent)

    result = reconcile_branch_drift(fake_task_input(repoPath=local, branch="main"))

    assert _completed(result)
    out = result.output_data
    assert out["mergeState"] == "conflicted"
    assert out["commit"] == ""
    # The merge was aborted: no lingering conflict markers, and the candidate's
    # own commit is untouched -- nothing unverified is left sitting on top of it.
    assert git.has_conflicts(local) == []
    assert git.head(local) == candidate_head
