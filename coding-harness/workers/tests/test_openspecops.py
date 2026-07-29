"""Unit tests for the OpenSpec-CLI-backed worker tasks and the tasks.md parser.

The subprocess boundary (``common.openspec_cli.run``) is mocked — no real ``openspec``
binary required — following the same pattern as ``test_github.py``'s ``RecordingRun``.
``common/tasks_md.py`` is pure Python and tested directly against sample markdown.
"""

from __future__ import annotations

import json

import pytest

import openspecops.tasks as openspecops_tasks
from common import openspec_cli, tool_policy
from common.tasks_md import TasksMdError, parse_tasks_md
from openspecops.tasks import (
    openspec_instructions,
    openspec_new_change,
    openspec_read_proposal,
    openspec_run_subtask_check,
    openspec_status,
    openspec_tasks_to_subtasks,
    openspec_validate_change,
)


class RecordingRun:
    """Drop-in for ``common.openspec_cli.run``: records each argv and returns
    queued RunResult-shaped stdout in order."""

    def __init__(self, *stdouts):
        self.calls: list[list[str]] = []
        self._stdouts = list(stdouts)

    def __call__(self, cmd, cwd=None, check=True):
        self.calls.append(cmd)
        stdout = self._stdouts.pop(0) if self._stdouts else "{}"

        class _Result:
            pass

        r = _Result()
        r.stdout = stdout
        return r


def _completed(result) -> bool:
    return result.status.value == "COMPLETED"


def _failed(result) -> bool:
    return result.status.value == "FAILED"


# --- openspec_new_change ------------------------------------------------------

def test_openspec_new_change_runs_cli_and_seeds_rule(monkeypatch, fake_task_input, tmp_path):
    (tmp_path / "openspec").mkdir()
    (tmp_path / "openspec" / "config.yaml").write_text("schema: spec-driven\n")
    rec = RecordingRun(json.dumps({
        "change": {"id": "add-x", "path": str(tmp_path / "openspec/changes/add-x"), "schema": "spec-driven"}
    }))
    monkeypatch.setattr(openspec_cli, "run", rec)
    task = fake_task_input(repoPath=str(tmp_path), name="add-x", description="Add X")
    result = openspec_new_change(task)
    assert _completed(result)
    assert result.output_data["changeName"] == "add-x"
    assert result.output_data["tasksRuleSeeded"] is True
    assert rec.calls[0][:3] == ["openspec", "new", "change"]
    import yaml
    cfg = yaml.safe_load((tmp_path / "openspec" / "config.yaml").read_text())
    assert cfg["rules"]["tasks"] == [openspec_cli.TASKS_RULE]


def test_openspec_new_change_rerun_does_not_reseed_rule(monkeypatch, fake_task_input, tmp_path):
    (tmp_path / "openspec").mkdir()
    (tmp_path / "openspec" / "config.yaml").write_text("schema: spec-driven\n")
    rec = RecordingRun(
        json.dumps({"change": {"id": "add-x", "path": "p", "schema": "spec-driven"}}),
        json.dumps({"change": {"id": "add-x", "path": "p", "schema": "spec-driven"}}),
    )
    monkeypatch.setattr(openspec_cli, "run", rec)
    task = fake_task_input(repoPath=str(tmp_path), name="add-x")
    openspec_new_change(task)
    result = openspec_new_change(task)
    assert _completed(result)
    assert result.output_data["tasksRuleSeeded"] is False


def test_openspec_new_change_fails_closed_on_bad_json(monkeypatch, fake_task_input, tmp_path):
    rec = RecordingRun("not json")
    monkeypatch.setattr(openspec_cli, "run", rec)
    task = fake_task_input(repoPath=str(tmp_path), name="add-x")
    result = openspec_new_change(task)
    assert _failed(result)


def test_openspec_new_change_slugifies_branch_shaped_name(monkeypatch, fake_task_input, tmp_path):
    """`changeBranch` values like `harness/issue-42` (a valid git branch name)
    must be coerced into a valid OpenSpec kebab-case slug before being passed
    to `openspec new change`, which rejects `/` outright."""
    rec = RecordingRun(json.dumps({
        "change": {"id": "harness-issue-42", "path": "p", "schema": "spec-driven"}
    }))
    monkeypatch.setattr(openspec_cli, "run", rec)
    task = fake_task_input(repoPath=str(tmp_path), name="harness/issue-42")
    result = openspec_new_change(task)
    assert _completed(result)
    assert rec.calls[0][:4] == ["openspec", "new", "change", "harness-issue-42"]


def test_tasks_rule_tells_the_planner_test_commands_must_be_a_single_invocation():
    """The planning agent must be told what the coding agent is actually
    permitted to run, or it writes Test: lines (shell pipes, &&, `test`, a bare
    `python`) that get denied or misparsed downstream — see design.md's
    post-implementation-fixes history for the live failures this prevents."""
    rule = openspec_cli.TASKS_RULE
    for phrase in ("exactly one program invocation", "python3", "pytest"):
        assert phrase in rule
    for forbidden in ("`|`", "`&&`", "`test`"):
        assert forbidden in rule


# --- slugify_change_name (pure function) -------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("add-x", "add-x"),
    ("harness/issue-42", "harness-issue-42"),
    ("Add Auth_Flow", "add-auth-flow"),
    ("feature/ABC-123_Fix bug", "feature-abc-123-fix-bug"),
    ("123-start-with-digit", "change-123-start-with-digit"),
    ("--leading-and-trailing--", "leading-and-trailing"),
    ("___", "change"),
])
def test_slugify_change_name(raw, expected):
    assert openspec_cli.slugify_change_name(raw) == expected


# --- openspec_status / openspec_instructions ---------------------------------

def test_openspec_status_returns_parsed_json(monkeypatch, fake_task_input, tmp_path):
    payload = {"changeName": "add-x", "applyRequires": ["tasks"],
               "artifacts": [{"id": "proposal", "status": "ready"}]}
    rec = RecordingRun(json.dumps(payload))
    monkeypatch.setattr(openspec_cli, "run", rec)
    task = fake_task_input(repoPath=str(tmp_path), changeName="add-x")
    result = openspec_status(task)
    assert _completed(result)
    assert result.output_data == payload
    assert rec.calls[0] == ["openspec", "status", "--change", "add-x", "--json"]


def test_openspec_instructions_returns_parsed_json(monkeypatch, fake_task_input, tmp_path):
    payload = {"artifactId": "proposal", "instruction": "Create it.", "template": "## Why",
               "resolvedOutputPath": "/tmp/x/proposal.md", "rules": ["keep it short"]}
    rec = RecordingRun(json.dumps(payload))
    monkeypatch.setattr(openspec_cli, "run", rec)
    task = fake_task_input(repoPath=str(tmp_path), changeName="add-x", artifact="proposal")
    result = openspec_instructions(task)
    assert _completed(result)
    assert result.output_data == payload
    assert rec.calls[0] == ["openspec", "instructions", "proposal", "--change", "add-x", "--json"]


# --- openspec_tasks_to_subtasks (worker wrapping common/tasks_md.py) --------

def test_openspec_tasks_to_subtasks_parses_file(fake_task_input, tmp_path):
    tasks_md = tmp_path / "tasks.md"
    tasks_md.write_text(
        "## 1. Setup\n\nFiles: a.py\nTest: pytest tests/test_a.py\n\n- [ ] 1.1 Do it\n"
    )
    task = fake_task_input(tasksPath=str(tasks_md))
    result = openspec_tasks_to_subtasks(task)
    assert _completed(result)
    assert result.output_data["subtasks"] == [
        {"id": "setup", "description": "1.1 Do it", "files": ["a.py"], "testCmd": "pytest tests/test_a.py"}
    ]


def test_openspec_tasks_to_subtasks_requires_path(fake_task_input):
    task = fake_task_input(tasksPath="")
    result = openspec_tasks_to_subtasks(task)
    assert _failed(result)


def test_openspec_tasks_to_subtasks_fails_closed_on_missing_file(fake_task_input, tmp_path):
    task = fake_task_input(tasksPath=str(tmp_path / "nope.md"))
    result = openspec_tasks_to_subtasks(task)
    assert _failed(result)


# --- parse_tasks_md (pure function) ------------------------------------------

def test_parse_tasks_md_splits_independent_groups():
    text = (
        "## 1. Setup\n\nFiles: a.py, b.py\nTest: pytest tests/test_setup.py\n\n"
        "- [ ] 1.1 Create module\n- [ ] 1.2 Add deps\n\n"
        "## 2. Core\n\nFiles: c.py\nTest: pytest tests/test_core.py\n\n"
        "- [ ] 2.1 Implement thing\n"
    )
    subtasks = parse_tasks_md(text)
    assert subtasks == [
        {"id": "setup", "description": "1.1 Create module\n1.2 Add deps",
         "files": ["a.py", "b.py"], "testCmd": "pytest tests/test_setup.py"},
        {"id": "core", "description": "2.1 Implement thing",
         "files": ["c.py"], "testCmd": "pytest tests/test_core.py"},
    ]


def test_parse_tasks_md_fails_closed_on_overlapping_files():
    text = (
        "## 1. Setup\n\nFiles: a.py\nTest: pytest tests/test_setup.py\n\n- [ ] 1.1 X\n\n"
        "## 2. Overlap\n\nFiles: a.py\nTest: pytest tests/test_overlap.py\n\n- [ ] 2.1 Y\n"
    )
    with pytest.raises(TasksMdError, match="file-disjoint"):
        parse_tasks_md(text)


def test_parse_tasks_md_fails_closed_on_missing_files_line():
    text = "## 1. Setup\n\nTest: pytest tests/test_setup.py\n\n- [ ] 1.1 X\n"
    with pytest.raises(TasksMdError, match="Files:"):
        parse_tasks_md(text)


def test_parse_tasks_md_fails_closed_on_missing_test_line():
    text = "## 1. Setup\n\nFiles: a.py\n\n- [ ] 1.1 X\n"
    with pytest.raises(TasksMdError, match="Test:"):
        parse_tasks_md(text)


def test_parse_tasks_md_dedupes_slug_collisions():
    text = (
        "## 1. Setup\n\nFiles: a.py\nTest: pytest a\n\n- [ ] 1.1 X\n\n"
        "## 2. Setup\n\nFiles: b.py\nTest: pytest b\n\n- [ ] 2.1 Y\n"
    )
    subtasks = parse_tasks_md(text)
    assert [s["id"] for s in subtasks] == ["setup", "setup-2"]


# --- superseded validate/archive tasks are removed ---------------------------

def test_bespoke_validate_and_archive_tasks_are_removed():
    import openspecops.tasks as openspecops_tasks
    assert not hasattr(openspecops_tasks, "openspec_change_validation")
    assert not hasattr(openspecops_tasks, "openspec_archive")
    assert not hasattr(openspec_cli, "validate_changes")
    assert not hasattr(openspec_cli, "archive")


# --- openspec_run_subtask_check (real command execution) --------------------

def test_openspec_run_subtask_check_reports_pass(fake_task_input, tmp_path):
    task = fake_task_input(repoPath=str(tmp_path), id="setup", testCmd="python3 -c \"print('ok')\"")
    result = openspec_run_subtask_check(task)
    assert _completed(result)
    assert result.output_data["passed"] is True
    assert result.output_data["exitCode"] == 0
    assert result.output_data["id"] == "setup"


def test_openspec_run_subtask_check_reports_failure(fake_task_input, tmp_path):
    task = fake_task_input(repoPath=str(tmp_path), id="setup", testCmd="python3 -c \"raise SystemExit(1)\"")
    result = openspec_run_subtask_check(task)
    assert _completed(result)
    assert result.output_data["passed"] is False
    assert result.output_data["exitCode"] == 1


def test_openspec_run_subtask_check_requires_repo_path(fake_task_input):
    task = fake_task_input(repoPath="", testCmd="pytest")
    result = openspec_run_subtask_check(task)
    assert _failed(result)


def test_openspec_run_subtask_check_passes_without_a_declared_command(fake_task_input, tmp_path):
    task = fake_task_input(repoPath=str(tmp_path), id="docs-only", testCmd="")
    result = openspec_run_subtask_check(task)
    assert _completed(result)
    assert result.output_data["passed"] is True


def test_openspec_run_subtask_check_reports_missing_binary_as_a_failed_check(fake_task_input, tmp_path):
    """A Test: command whose binary isn't on PATH (e.g. `python` on a host that
    only has `python3`) must surface as passed=false so the verification loop's
    fixup pass gets a turn — not as a worker-level task failure that aborts the
    whole FORK_JOIN_DYNAMIC and the run with it."""
    task = fake_task_input(repoPath=str(tmp_path), id="setup",
                           testCmd="definitely-not-a-real-binary-xyz --version")
    result = openspec_run_subtask_check(task)
    assert _completed(result)
    assert result.output_data["passed"] is False
    assert "definitely-not-a-real-binary-xyz" in result.output_data["log"]


# --- openspec_read_proposal ---------------------------------------------------

def test_openspec_read_proposal_returns_exact_content(fake_task_input, tmp_path):
    change_dir = tmp_path / "openspec" / "changes" / "add-x"
    change_dir.mkdir(parents=True)
    (change_dir / "proposal.md").write_text("## Why\n\nBecause reasons.\n")
    task = fake_task_input(changeDir=str(change_dir))
    result = openspec_read_proposal(task)
    assert _completed(result)
    assert result.output_data["proposalText"] == "## Why\n\nBecause reasons.\n"


def test_openspec_read_proposal_fails_closed_on_missing_file(fake_task_input, tmp_path):
    task = fake_task_input(changeDir=str(tmp_path / "nope"))
    result = openspec_read_proposal(task)
    assert _failed(result)


# --- openspec_validate_change --------------------------------------------------

class _FakeCompletedProcess:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_openspec_validate_change_reports_valid(monkeypatch, fake_task_input, tmp_path):
    payload = {"items": [{"id": "add-x", "type": "change", "valid": True, "issues": []}]}

    def fake_run(cmd, **kwargs):
        assert cmd[:3] == [openspec_cli.BIN, "validate", "add-x"]
        assert "--strict" in cmd and "--no-interactive" in cmd and "--json" in cmd
        return _FakeCompletedProcess(0, stdout=json.dumps(payload))

    monkeypatch.setattr(openspecops_tasks.subprocess, "run", fake_run)
    task = fake_task_input(repoPath=str(tmp_path), changeId="add-x")
    result = openspec_validate_change(task)
    assert _completed(result)
    assert result.output_data["valid"] is True
    assert result.output_data["issues"] == []


def test_openspec_validate_change_reports_invalid_without_raising(monkeypatch, fake_task_input, tmp_path):
    """A broken proposal/design/specs/tasks.md must surface as a failed check the
    verification loop's fixup pass can act on — never a worker crash — or the
    workflow dies at `archive_change` (openspec_finalize) time instead."""
    payload = {"items": [{"id": "add-x", "type": "change", "valid": False,
                          "issues": [{"message": "missing Scenario for Requirement X"}]}]}

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(1, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(openspecops_tasks.subprocess, "run", fake_run)
    task = fake_task_input(repoPath=str(tmp_path), changeId="add-x")
    result = openspec_validate_change(task)
    assert _completed(result)
    assert result.output_data["valid"] is False
    assert result.output_data["issues"] == [{"message": "missing Scenario for Requirement X"}]


def test_openspec_validate_change_requires_repo_path_and_change_id(fake_task_input):
    task = fake_task_input(repoPath="", changeId="")
    result = openspec_validate_change(task)
    assert _failed(result)


# --- tool_policy.allowed_tools_for_test_command ------------------------------

def test_allow_pattern_for_default_covered_command_is_a_noop():
    assert tool_policy.allowed_tools_for_test_command("pytest tests/test_a.py") == \
        tool_policy.DEFAULT_ALLOWED_TOOLS


def test_allow_pattern_for_non_default_command_is_appended():
    allowed = tool_policy.allowed_tools_for_test_command("make check")
    assert allowed[:-1] == tool_policy.DEFAULT_ALLOWED_TOOLS
    assert allowed[-1] == "Bash(make *)"


def test_allow_pattern_documented_compound_command_limitation():
    # `cd` is not itself an allowed token, so a chained command's first token
    # ("cd") produces a pattern that won't cover the rest of the chain — a known,
    # documented limitation rather than something this helper resolves.
    pattern = tool_policy.test_command_allow_pattern("cd tests && pytest")
    assert pattern == "Bash(cd *)"
