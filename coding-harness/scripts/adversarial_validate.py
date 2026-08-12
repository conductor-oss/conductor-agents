#!/usr/bin/env python3
"""Adversarial runtime validation independent of the repository's test suite."""

from __future__ import annotations

import os
import json
import random
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))

from campaign.model import validate_plan  # noqa: E402
from coding_agent.tasks import (  # noqa: E402
    _path_state,
    _reconcile_agent_changes,
    _within_write_roots,
)
from common import code_parallel, git, test_plan, verification  # noqa: E402
from common.exec import RunError  # noqa: E402
from gitops.tasks import _publication_block  # noqa: E402
from openspecops.tasks import _validated_subtasks  # noqa: E402


def command(*args: str, cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


def initialize_repo(path: Path) -> None:
    path.mkdir(parents=True)
    command("git", "init", "-q", "-b", "main", cwd=path)
    command("git", "config", "user.name", "Harness Adversary", cwd=path)
    command("git", "config", "user.email", "adversary@example.invalid", cwd=path)


def commit_all(path: Path, message: str) -> str:
    command("git", "add", "-A", cwd=path)
    command("git", "commit", "-q", "-m", message, cwd=path)
    return command("git", "rev-parse", "HEAD", cwd=path)


def validate_dirty_scope_reconciliation(root: Path) -> int:
    repo = root / "scope-repo"
    initialize_repo(repo)
    (repo / "allowed.txt").write_text("initial allowed\n", encoding="utf-8")
    (repo / "dirty.txt").write_text("initial dirty\n", encoding="utf-8")
    commit_all(repo, "initial")

    # Simulate caller-owned staged and unstaged edits before the agent begins.
    (repo / "dirty.txt").write_text("caller staged\n", encoding="utf-8")
    command("git", "add", "dirty.txt", cwd=repo)
    (repo / "dirty.txt").write_text("caller worktree\n", encoding="utf-8")
    before_codes = git.status_changes(str(repo), untracked_files_all=True)
    before_states = {path: _path_state(str(repo), path) for path in before_codes}
    index_before = git.index_snapshot(str(repo))
    branch_before = command("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    head_before = git.head(str(repo))

    # Simulate a hostile backend using shell access to modify an already-dirty
    # forbidden path, create another forbidden path, and stage everything.
    (repo / "allowed.txt").write_text("authorized agent edit\n", encoding="utf-8")
    (repo / "dirty.txt").write_text("unauthorized agent overwrite\n", encoding="utf-8")
    (repo / "escaped.txt").write_text("unauthorized new file\n", encoding="utf-8")
    command("git", "add", "-A", cwd=repo)

    outcome = _reconcile_agent_changes(
        str(repo), ["allowed.txt"], before_states, index_before,
        branch_before, head_before)
    assert outcome["indexChanged"] is True
    assert outcome["unauthorized"] == ["dirty.txt", "escaped.txt"]
    assert (repo / "allowed.txt").read_text(encoding="utf-8") == "authorized agent edit\n"
    assert (repo / "dirty.txt").read_text(encoding="utf-8") == "caller worktree\n"
    assert not (repo / "escaped.txt").exists()
    assert git.index_snapshot(str(repo))[1] == index_before[1]
    return 1


def validate_committed_scope_bypass(root: Path) -> int:
    repo = root / "commit-scope-repo"
    initialize_repo(repo)
    (repo / "allowed.txt").write_text("initial allowed\n", encoding="utf-8")
    (repo / "forbidden.txt").write_text("initial forbidden\n", encoding="utf-8")
    initial = commit_all(repo, "initial")
    index_before = git.index_snapshot(str(repo))

    (repo / "allowed.txt").write_text("authorized committed edit\n", encoding="utf-8")
    (repo / "forbidden.txt").write_text("hidden forbidden commit\n", encoding="utf-8")
    commit_all(repo, "agent tried to commit")
    outcome = _reconcile_agent_changes(
        str(repo), ["allowed.txt"], {}, index_before, "main", initial)
    assert outcome["headChanged"] is True and outcome["branchChanged"] is False
    assert git.head(str(repo)) == initial
    assert (repo / "allowed.txt").read_text(encoding="utf-8") == "authorized committed edit\n"
    assert (repo / "forbidden.txt").read_text(encoding="utf-8") == "initial forbidden\n"
    assert outcome["unauthorized"] == ["forbidden.txt"]

    # A branch switch is itself terminal and must return to the original branch.
    command("git", "restore", "allowed.txt", cwd=repo)
    index_before = git.index_snapshot(str(repo))
    command("git", "switch", "-q", "-c", "agent-illicit-branch", cwd=repo)
    (repo / "allowed.txt").write_text("edit on wrong branch\n", encoding="utf-8")
    commit_all(repo, "wrong branch")
    switched = _reconcile_agent_changes(
        str(repo), ["allowed.txt"], {}, index_before, "main", initial)
    assert switched["branchChanged"] is True
    assert command("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main"
    assert git.head(str(repo)) == initial
    assert (repo / "allowed.txt").read_text(encoding="utf-8") == "initial allowed\n"
    return 2


def validate_porcelain_paths(root: Path) -> int:
    repo = root / "path-repo"
    initialize_repo(repo)
    old = "old -> name.txt"
    renamed = "new line\nname.txt"
    untracked = 'quote " and newline\n.txt'
    (repo / old).write_text("tracked\n", encoding="utf-8")
    commit_all(repo, "weird name")
    command("git", "mv", old, renamed, cwd=repo)
    (repo / untracked).write_text("untracked\n", encoding="utf-8")
    files = git.status_files(str(repo))
    changes = git.status_changes(str(repo), untracked_files_all=True)
    assert renamed in files and old not in files
    assert changes[renamed] == "R"
    assert changes[untracked] == "A"
    return 1


def validate_workspace_branch_outcomes(root: Path) -> int:
    inplace = root / "inplace-repo"
    initialize_repo(inplace)
    (inplace / "tracked.txt").write_text("original\n", encoding="utf-8")
    main_head = commit_all(inplace, "initial")
    (inplace / "tracked.txt").write_text("caller edit\n", encoding="utf-8")
    (inplace / "untracked.txt").write_text("caller new\n", encoding="utf-8")
    prepared = git.prepare_inplace(str(inplace), run_id="workflow-123")
    assert prepared["branch"] != "main"
    assert prepared["originalBranch"] == "main"
    assert prepared["originalHead"] == main_head
    assert command("git", "rev-parse", "main", cwd=inplace) == main_head
    assert set(prepared["includedPaths"]) == {"tracked.txt", "untracked.txt"}
    assert git.status_files(str(inplace)) == set()
    assert (inplace / "tracked.txt").read_text(encoding="utf-8") == "caller edit\n"
    assert (inplace / "untracked.txt").read_text(encoding="utf-8") == "caller new\n"

    isolated = root / "isolated-repo"
    initialize_repo(isolated)
    (isolated / "tracked.txt").write_text("original\n", encoding="utf-8")
    isolated_head = commit_all(isolated, "initial")
    (isolated / "tracked.txt").write_text("caller edit\n", encoding="utf-8")
    (isolated / "untracked.txt").write_text("caller new\n", encoding="utf-8")
    status_before = git.status_fingerprint(str(isolated))
    index_before = git.index_snapshot(str(isolated))[1]
    snapshot = git.snapshot_worktree(
        str(isolated), run_id="workflow-456", original_branch="main",
        original_head=isolated_head)
    assert command("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=isolated) == "main"
    assert git.head(str(isolated)) == isolated_head
    assert git.status_fingerprint(str(isolated)) == status_before
    assert git.index_snapshot(str(isolated))[1] == index_before
    workspace = git.workspace_add(
        str(isolated), "workflow-456", start_point=snapshot["baselineCommit"])
    workspace_path = Path(workspace["worktreePath"])
    assert workspace["branch"] != "main"
    assert (workspace_path / "tracked.txt").read_text(encoding="utf-8") == "caller edit\n"
    assert (workspace_path / "untracked.txt").read_text(encoding="utf-8") == "caller new\n"
    assert command("git", "rev-parse", "main", cwd=isolated) == isolated_head
    return 2


def reference_normalize(value: object) -> str | None:
    raw = str(value or "").replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        return None
    stack: list[str] = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not stack:
                return None
            stack.pop()
        else:
            stack.append(part)
    return "/".join(stack) or None


def validate_path_properties() -> int:
    generator = random.Random(0xC0D3C7)
    atoms = ["src", "test", ".env", "a b", "..", ".", "nested", "x-y", "C:", ""]
    for _ in range(10_000):
        path = "/".join(generator.choice(atoms) for _ in range(generator.randint(1, 5)))
        root = "/".join(generator.choice(atoms) for _ in range(generator.randint(1, 3)))
        if generator.random() < 0.2:
            path = path.replace("/", "\\")
        if generator.random() < 0.1:
            path = "/" + path
        norm_path, norm_root = reference_normalize(path), reference_normalize(root)
        expected = bool(norm_path and norm_root and
                        (norm_path == norm_root or norm_path.startswith(norm_root + "/")))
        actual = _within_write_roots(path, [root])
        assert actual == expected, (path, root, norm_path, norm_root, actual, expected)
    assert _within_write_roots(".env", ["env"]) is False
    assert _within_write_roots("src/../.env", [".env"]) is True
    assert _within_write_roots("../../outside", ["."]) is False
    return 10_003


def validate_planner_boundaries(root: Path) -> int:
    repo = root / "planner-repo"
    initialize_repo(repo)
    (repo / "src").mkdir()
    valid = [{
        "id": "implement-api",
        "description": "Implement API",
        "files": ["src/api.py"],
    }]
    normalized = _validated_subtasks(valid, str(repo))
    assert normalized[0]["id"] == valid[0]["id"]

    # Testing is a separate concern this validator does not represent -- it is
    # test_cycle's job now, gated by its own argv validators -- so a legacy or
    # hostile testCommands/testCmd field must be silently stripped here, never
    # preserved or executed.
    tainted = _validated_subtasks(
        [{**valid[0], "testCommands": ["pytest tests/test_api.py && rm -rf output"]}], str(repo))
    assert "testCommands" not in tainted[0]
    tainted = _validated_subtasks([{**valid[0], "testCmd": "rm -rf /"}], str(repo))
    assert "testCmd" not in tainted[0]

    invalid_variants = [
        [{**valid[0], "id": "Not A Slug"}],
        [valid[0], {**valid[0]}],
        [{**valid[0], "files": ["../escape.py"]}],
        [{**valid[0], "files": ["/absolute.py"]}],
        [{**valid[0], "files": ["src"]}],
    ]
    for variant in invalid_variants:
        try:
            _validated_subtasks(variant, str(repo))
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe parallel plan was accepted: {variant}")

    generator = random.Random(0x51A6)
    for _ in range(2_000):
        raw = {"tasks": []}
        for position in range(generator.randint(0, 8)):
            raw["tasks"].append({
                "id": generator.choice(["Task Name", "ok_task", "../bad", "", f"task-{position}"]),
                "description": generator.choice(["work", "", "verify"]),
                "dependsOn": [],
                "files": [generator.choice(["src/a.py", ".env", "../escape", "/tmp/bad", "src/*.py"])],
                "acceptanceCriteria": generator.choice([["done"], []]),
                "checks": generator.choice([["pytest tests/test_a.py"], []]),
            })
        result = validate_plan(raw)
        if result["valid"]:
            tasks = result["tasks"]
            ids = [task["id"] for task in tasks]
            assert len(ids) == len(set(ids))
            assert all(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", ident) for ident in ids)
            assert all(task["description"] and task["files"] and task["acceptanceCriteria"]
                       for task in tasks)
            assert all(reference_normalize(path) == path for task in tasks for path in task["files"])
    return 2_008


def validate_verification_commands() -> int:
    focused = [
        ["./gradlew", ":module:test"],
        ["./gradlew", "test", "--tests", "example.UnitTest"],
        ["mvn", "-pl", "module", "test"],
        ["npm", "test", "--workspace", "package-a"],
        ["pnpm", "--filter", "package-a", "test"],
        ["yarn", "workspace", "package-a", "test"],
        ["pytest", "tests/unit/test_api.py"],
        ["go", "test", "./pkg/api"],
        ["cargo", "test", "-p", "api"],
        ["cmake", "-S", ".", "-B", ".conductor-verify-build"],
        ["cmake", "--build", ".conductor-verify-build", "--target", "api_test"],
        ["ctest", "--test-dir", ".conductor-verify-build", "-R", "api_test"],
        ["swift", "test", "--filter", "ApiTests"],
    ]
    broad = [
        ["./gradlew", "test"], ["mvn", "test"], ["npm", "test"],
        ["pnpm", "test"], ["yarn", "test"], ["pytest", "tests"],
        ["go", "test", "./..."], ["cargo", "test"],
        ["cmake", "--build", ".conductor-verify-build"],
        ["ctest", "--test-dir", ".conductor-verify-build"], ["swift", "test"],
    ]
    for argv in focused:
        assert verification.validate_remediation_argv(argv) == argv
    for argv in broad:
        try:
            verification.validate_remediation_argv(argv)
        except verification.VerificationBlocked:
            pass
        else:
            raise AssertionError(f"repository-wide remediation command accepted: {argv}")
    for token in ("&&", "|", ";", ">", "$(touch bad)", "`touch bad`"):
        try:
            verification.validate_remediation_argv(["pytest", "tests/test_api.py", token])
        except verification.VerificationBlocked:
            pass
        else:
            raise AssertionError(f"shell syntax accepted: {token}")
    return len(focused) + len(broad) + 6


def validate_workflow_transforms() -> int:
    """Exercise the Python logic that replaced this repository's last jq tasks.

    There are zero registered JSON_JQ_TRANSFORM tasks left anywhere in this
    repository (see AGENTS.md's JQ Usage Policy), so these three decisions now
    live in ``common.code_parallel``/``common.test_plan`` and are called
    directly rather than shelling out to ``jq`` against a `queryExpression`
    that no longer exists.
    """
    built = code_parallel.build_forks(
        repo_path="/repo", change_dir="design", code_model="model",
        code_prompt_template="", code_prompt_template_source="",
        spec_context_path="", context_paths=[], max_turns=10, max_budget_usd=1,
        model_profile="", model_policy={}, model_policy_source="",
        model_policy_sha256="", models_config="", model_overrides={},
        subtasks=[{
            "id": "multi-runtime", "description": "Change two runtimes",
            "files": ["src/a.py"],
            # A stray legacy testCommands/testCmd field (plan_validation
            # already strips both before build_forks ever sees a plan) must
            # never expand this subtask's tool grant: allowedTools is now a
            # fixed, safe list, not derived from anything a plan entry claims.
            "testCmd": "pytest tests/test_a.py",
            "testCommands": ["pytest tests/test_a.py", "swift test --filter ApiTests"],
        }],
    )
    subtask = built["dynamicTasksInput"]["multi-runtime"]
    assert subtask["allowedTools"] == code_parallel.CODE_SUBTASK_TOOLS
    assert not any("swift" in tool or "pytest" in tool for tool in subtask["allowedTools"])
    assert subtask["allowedWriteRoots"] == ["src/a.py"]
    assert built["dynamicTasks"][0]["subWorkflowParam"] == {"name": "code_subtask", "version": 1}
    assert built["groupIds"] == "multi-runtime"

    passed = code_parallel.resolve_verification_outcome(
        candidate_commit="a" * 40, delivery={"state": "passed"}, issues=[],
        merge_state="merged", plan_valid=True, tested="a" * 40,
        test_state="tests_passed", tests_passed=True)
    partial = code_parallel.resolve_verification_outcome(
        candidate_commit="b" * 40, delivery={"state": "passed"}, issues=[],
        merge_state="partial", plan_valid=True, tested=None,
        test_state="tests_passed", tests_passed=True)
    assert passed["passed"] is True and passed["state"] == "passed"
    assert partial["passed"] is False and partial["executionOutcome"] == "merge_blocked"

    plan = test_plan.resolve_plan(
        discovered=[{"argv": ["pytest", "tests/test_new.py"], "scope": "focused"}],
        prior={"commands": [{"argv": ["go", "test", "./pkg/api"], "scope": "focused"},
                            {"argv": ["pytest", "tests/test_new.py"], "scope": "focused"}]},
        user=None, discovery_outcome="discovered", discovery_reason="", selection=None)
    # Discovery's own findings lead, followed by whatever carried obligation
    # discovery did not already surface; an exact duplicate collapses to one.
    assert [item["argv"] for item in plan["commands"]] == [
        ["pytest", "tests/test_new.py"], ["go", "test", "./pkg/api"]]
    return 3


def validate_environment_outcomes(root: Path) -> int:
    capabilities = verification.runtime_capabilities()
    # Gradle/Maven need only the java family already probed here (the wrapper
    # scripts run under whatever JDK that resolves); pytest/cargo/make/cmake/
    # ctest/swift are probed by verification.py itself only when a discovered
    # command actually needs them, not as a standing family here.
    expected = {
        "java", "go", "python", "python3", "ruby", "bundle", "rake",
        "dotnet", "php", "composer", "mix", "sbt", "bazel",
        "node", "npm", "npx", "pnpm", "yarn",
    }
    assert set(capabilities) == expected
    for runtime, evidence in capabilities.items():
        assert isinstance(evidence.get("available"), bool), (runtime, evidence)
        if evidence["available"]:
            assert evidence.get("executable"), (runtime, evidence)
        else:
            assert evidence.get("reason"), (runtime, evidence)

    repo = root / "missing-runtime-repo"
    initialize_repo(repo)
    (repo / "source.txt").write_text("base\n", encoding="utf-8")
    commit_all(repo, "base")
    (repo / "source.txt").write_text("candidate\n", encoding="utf-8")
    candidate = commit_all(repo, "candidate")
    outcome = verification.verify_candidate(str(repo), candidate, [{
        "argv": ["definitely-missing-harness-runtime"],
        "source": "adversarial-configured-adapter",
        "adapter": "configured",
        "scope": "focused",
        "affectedUnit": "source.txt",
        "coveredPaths": ["source.txt"],
    }])
    assert outcome["verificationState"] == "blocked"
    assert outcome["executionOutcome"] == "infra_blocked"
    assert outcome["commands"][0]["blocked"] is True
    assert "executable not found" in outcome["commands"][0]["output"]
    return len(expected) + 1


def validate_real_push_failures(root: Path) -> int:
    repo = root / "push-repo"
    remote = root / "remote.git"
    initialize_repo(repo)
    (repo / "change.txt").write_text("one\n", encoding="utf-8")
    verified = commit_all(repo, "verified candidate")
    command("git", "init", "-q", "--bare", str(remote), cwd=root)
    command("git", "remote", "add", "origin", str(remote), cwd=repo)

    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "echo 'refusing to allow an OAuth App to create or update workflow without workflow scope' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    try:
        git.push(str(repo), branch_name="main", remote="origin", expected_head=verified)
    except RunError as exc:
        classified = _publication_block(exc)
        assert classified and classified[0] == "permission_blocked"
    else:
        raise AssertionError("rejecting remote unexpectedly accepted a push")
    assert git.head(str(repo)) == verified
    remote_ref = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/main"],
        cwd=remote,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    assert remote_ref.returncode != 0

    # Candidate binding must stop a local race before transport is attempted.
    (repo / "change.txt").write_text("two\n", encoding="utf-8")
    moved = commit_all(repo, "post-verification mutation")
    try:
        git.push(str(repo), branch_name="main", remote="origin", expected_head=verified)
    except ValueError as exc:
        assert "moved after candidate verification" in str(exc)
    else:
        raise AssertionError("moved local branch was published under stale verification")
    assert git.head(str(repo)) == moved
    return 2


def main() -> int:
    scenarios = 0
    with tempfile.TemporaryDirectory(prefix="harness-adversarial-") as directory:
        root = Path(directory)
        scenarios += validate_dirty_scope_reconciliation(root)
        scenarios += validate_committed_scope_bypass(root)
        scenarios += validate_porcelain_paths(root)
        scenarios += validate_workspace_branch_outcomes(root)
        scenarios += validate_path_properties()
        scenarios += validate_planner_boundaries(root)
        scenarios += validate_verification_commands()
        scenarios += validate_workflow_transforms()
        scenarios += validate_environment_outcomes(root)
        scenarios += validate_real_push_failures(root)
    print(f"adversarial runtime validation passed: {scenarios} generated/fault-injected cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
