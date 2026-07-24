from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from coding_agent.tasks import _append_context_files
from common import git
from gitops.tasks import workspace_cleanup, workspace_prepare
from openspec import tasks as openspec


def _dag_task(ident: str, *, deps=None, files=None, refs=None):
    return {
        "id": ident,
        "description": f"implement {ident}",
        "dependsOn": deps or [],
        "files": files or [f"src/{ident}.py"],
        "acceptanceCriteria": [f"{ident} works"],
        "checks": [f"pytest tests/test_{ident}.py"],
        "openspecTaskRefs": refs or [f"tasks.md#{ident}"],
    }


def test_source_type_supports_local_git_and_public_archive(tmp_path):
    local = tmp_path / "specs"
    local.mkdir()
    assert openspec._source_type(str(local), "auto", str(tmp_path)) == "local"
    assert openspec._source_type("https://github.com/acme/specs.git", "auto", str(tmp_path)) == "git"
    assert openspec._source_type("https://example.com/specs.tgz", "auto", str(tmp_path)) == "url"
    with pytest.raises(ValueError, match="infer"):
        openspec._source_type("https://example.com/specs", "auto", str(tmp_path))


def test_archive_extraction_rejects_path_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.txt", "bad")
    with pytest.raises(ValueError, match="unsafe archive path"):
        openspec._extract(archive, tmp_path / "out")


def test_sources_reject_inline_credentials():
    with pytest.raises(ValueError, match="inline credentials"):
        openspec._reject_inline_credentials("https://user:secret@example.com/specs.git")
    with pytest.raises(ValueError, match="query"):
        openspec._reject_inline_credentials("https://example.com/specs.zip?token=secret")


def test_route_selects_parallel_only_for_safe_single_wave(fake_task_input):
    assessment = {
        "recommendedMode": "parallel", "confidence": 0.95, "rationale": "small",
        "risks": [], "tasks": [_dag_task("api"), _dag_task("docs")],
    }
    result = openspec.openspec_route(fake_task_input(
        assessment=assessment, executionMode="auto", changeId="add-api",
        maxTasks=25, maxParallelism=6,
    )).output_data
    assert result["selectedMode"] == "parallel"
    assert result["parallelPlan"]["subtasks"][0]["testCmd"].startswith("pytest")
    assert "tasks.md#api" in result["parallelPlan"]["subtasks"][0]["description"]

    assessment["tasks"][1]["dependsOn"] = ["api"]
    result = openspec.openspec_route(fake_task_input(
        assessment=assessment, executionMode="auto", changeId="add-api",
    )).output_data
    assert result["selectedMode"] == "campaign"


def test_forced_parallel_rejects_overlapping_files(fake_task_input):
    assessment = {
        "recommendedMode": "parallel", "confidence": 1, "rationale": "",
        "risks": [], "tasks": [
            _dag_task("a", files=["src/core"]),
            _dag_task("b", files=["src/core/api.py"]),
        ],
    }
    result = openspec.openspec_route(fake_task_input(
        assessment=assessment, executionMode="parallel", changeId="unsafe",
    ))
    assert str(result.status.value) == "FAILED"
    assert "file-disjoint" in result.reason_for_incompletion


def test_intake_validates_apply_ready_local_change(monkeypatch, tmp_git_repo, fake_task_input, tmp_path):
    change = tmp_git_repo / "openspec" / "changes" / "add-greeting"
    change.mkdir(parents=True)
    (change / "proposal.md").write_text("# Greeting\n")
    (change / "tasks.md").write_text("- [ ] Add greeting\n")
    monkeypatch.setenv("OPENSPEC_SNAPSHOT_DIR", str(tmp_path / "snapshots"))

    def fake_run(args, *, cwd, allow_failure=False):
        if args[:2] == ["status", "--change"]:
            data = {"isComplete": True}
        elif args[:2] == ["instructions", "apply"]:
            data = {"tasks": [{"id": "1", "description": "Add greeting"}]}
        else:
            data = {"valid": True}
        return {"exitCode": 0, "data": data, "stdout": json.dumps(data), "stderr": ""}

    monkeypatch.setattr(openspec, "_run", fake_run)
    result = openspec.openspec_intake(fake_task_input(
        repoPath=str(tmp_git_repo), specSource=".", specSourceType="local",
        changeId="add-greeting", workflowId="wf-local",
    )).output_data
    assert result["valid"] is True and result["sameRepo"] is True
    context = Path(result["contextPath"]).read_text()
    assert "proposal.md" in context and "Add greeting" in context
    assert result["provenance"]["sha256"]


def test_local_source_is_materialized_into_owned_worktree(monkeypatch, tmp_git_repo, fake_task_input, tmp_path):
    source = tmp_git_repo / "design" / "openspec"
    change = source / "changes" / "add-greeting"
    change.mkdir(parents=True)
    (change / "proposal.md").write_text("# Greeting\n")
    (change / "tasks.md").write_text("- [ ] Add greeting\n")
    monkeypatch.setenv("OPENSPEC_SNAPSHOT_DIR", str(tmp_path / "snapshots"))

    def fake_run(args, *, cwd, allow_failure=False):
        if args[:2] == ["status", "--change"]:
            data = {"isComplete": True}
        elif args[:2] == ["instructions", "apply"]:
            data = {"tasks": [{"id": "1", "description": "Add greeting"}]}
        else:
            data = {"valid": True}
        return {"exitCode": 0, "data": data, "stdout": json.dumps(data), "stderr": ""}

    monkeypatch.setattr(openspec, "_run", fake_run)
    resolved = openspec.openspec_source_resolve(fake_task_input(
        repoPath=str(tmp_git_repo), specSource=str(source), specSourceType="local",
        changeId="add-greeting", useSpecSourceWorkspace=False,
    )).output_data
    assert resolved["materializeLocalSource"] is True
    workspace = workspace_prepare(fake_task_input(
        repoPath=resolved["workspaceRepoPath"], workflowId="wf-materialize", branch="openspec/test",
        materializedSourcePaths=[resolved["sourceOpenSpecRelativePath"]])).output_data
    result = openspec.openspec_intake(fake_task_input(
        repoPath=workspace["worktreePath"], specSource=str(source), specSourceType="local",
        changeId="add-greeting", workflowId="wf-materialize", workspaceOwned=True,
        sourceResolution=resolved,
    )).output_data
    mapped = Path(workspace["worktreePath"]) / "design" / "openspec" / "changes" / "add-greeting"
    assert mapped.joinpath("tasks.md").read_text() == "- [ ] Add greeting\n"
    assert result["writebackRepoPath"] == workspace["worktreePath"]
    assert result["writebackProjectPath"] == str(Path(workspace["worktreePath"]) / "design")
    assert result["forceAddPaths"] == ["design/openspec"]
    assert result["publishOnVerify"] is False
    workspace_cleanup(fake_task_input(
        sourceRepoPath=workspace["sourceRepoPath"], worktreePath=workspace["worktreePath"],
        owned=True, keepWorktree=False, outcome="verified"))


def test_local_source_workspace_can_replace_target_and_publish(tmp_git_repo, fake_task_input):
    source = tmp_git_repo / "openspec"
    change = source / "changes" / "add-greeting"
    change.mkdir(parents=True)
    (change / "proposal.md").write_text("# Greeting\n")
    (change / "tasks.md").write_text("- [ ] Add greeting\n")
    result = openspec.openspec_source_resolve(fake_task_input(
        repoPath="", specSource=str(source), specSourceType="local", changeId="add-greeting",
        useSpecSourceWorkspace=True,
    )).output_data
    assert result["workspaceRepoPath"] == str(tmp_git_repo)
    assert result["materializeLocalSource"] is True
    assert result["publishOnVerify"] is True


def test_force_add_commits_an_ignored_openspec_tree(tmp_git_repo):
    (tmp_git_repo / ".gitignore").write_text("design/\n")
    git.git(str(tmp_git_repo), "add", ".gitignore")
    git.git(str(tmp_git_repo), "commit", "-m", "ignore design")
    artifact = tmp_git_repo / "design" / "openspec" / "changes" / "archive" / "2026-add-greeting"
    artifact.mkdir(parents=True)
    (artifact / "tasks.md").write_text("- [x] Add greeting\n")
    git.commit(str(tmp_git_repo), "openspec archive", force_add_paths=["design/openspec"])
    assert git.git(str(tmp_git_repo), "ls-files", "design/openspec/changes/archive/2026-add-greeting/tasks.md").stdout.strip()


def test_coding_context_is_read_only_bounded_and_ignores_empty(monkeypatch, tmp_path):
    root = tmp_path / "snapshots"
    root.mkdir()
    context = root / "context.md"
    context.write_text("authoritative spec")
    monkeypatch.setenv("OPENSPEC_SNAPSHOT_DIR", str(root))
    assert _append_context_files("prompt", [""]) == "prompt"
    assert "authoritative spec" in _append_context_files("prompt", [str(context)])
    outside = tmp_path / "outside.md"
    outside.write_text("no")
    with pytest.raises(ValueError, match="outside"):
        _append_context_files("prompt", [str(outside)])


def test_complete_tasks_marks_every_open_checkbox(tmp_path):
    path = tmp_path / "tasks.md"
    path.write_text("- [ ] first\n- [x] done\n  - [ ] nested\n")
    assert openspec._complete_tasks(path) == 2
    assert path.read_text() == "- [x] first\n- [x] done\n  - [x] nested\n"


def test_pinned_cli_accepts_a_complete_spec_driven_change(tmp_path):
    binary = Path(openspec._openspec_bin())
    if not binary.is_file():
        pytest.skip("run ./run.sh setup to install the pinned OpenSpec CLI")
    subprocess.run([str(binary), "init", str(tmp_path), "--tools", "none"],
                   check=True, capture_output=True, text=True)
    subprocess.run([str(binary), "new", "change", "add-greeting"], cwd=tmp_path,
                   check=True, capture_output=True, text=True)
    change = tmp_path / "openspec" / "changes" / "add-greeting"
    (change / "proposal.md").write_text(
        "## Why\nUsers need a greeting.\n\n## What Changes\n- Add greeting output.\n\n"
        "## Capabilities\n\n### New Capabilities\n- `greeting`: Return a greeting.\n\n"
        "### Modified Capabilities\n\n## Impact\nA small additive API.\n")
    (change / "design.md").write_text(
        "## Context\nA small feature.\n\n## Goals / Non-Goals\n- Add greeting.\n\n"
        "## Decisions\nUse a pure function.\n\n## Risks / Trade-offs\nLow risk.\n")
    (change / "tasks.md").write_text("## 1. Implementation\n\n- [ ] 1.1 Add greeting and tests\n")
    spec = change / "specs" / "greeting"
    spec.mkdir(parents=True)
    (spec / "spec.md").write_text(
        "## ADDED Requirements\n\n### Requirement: Greeting\n"
        "The system SHALL return a greeting.\n\n#### Scenario: Default greeting\n"
        "- **WHEN** a greeting is requested\n- **THEN** the system returns a non-empty greeting\n")
    status = subprocess.run([str(binary), "status", "--change", "add-greeting", "--json"],
                            cwd=tmp_path, check=True, capture_output=True, text=True)
    assert json.loads(status.stdout)["isComplete"] is True
    subprocess.run([str(binary), "validate", "add-greeting", "--type", "change", "--strict",
                    "--no-interactive", "--json"], cwd=tmp_path, check=True,
                   capture_output=True, text=True)
