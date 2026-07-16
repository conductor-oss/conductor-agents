"""Unit tests for the ``run_checks`` worker + its ``_detect_default`` helper.

Mirrors ``test_gitops.py`` style: real subprocess (``bash``) against throwaway
dirs, no network, no LLM. The central contract under test is *fail-soft* — a
failing check is a COMPLETED task carrying ``passed=False``, not a FAILED task.
Only genuine worker errors (missing/bad ``repoPath``) return FAILED.
"""

from __future__ import annotations

from pathlib import Path

from checks.tasks import run_checks, _detect_default, OUTPUT_CAP


def _completed(result) -> bool:
    return result.status.value == "COMPLETED"


# --- supplied command paths -------------------------------------------------

def test_supplied_cmd_pass(fake_task_input, tmp_git_repo):
    result = run_checks(fake_task_input(repoPath=str(tmp_git_repo), cmd="true"))
    assert _completed(result)
    out = result.output_data
    assert out["passed"] is True
    assert out["ran"] is True
    assert out["detected"] is False
    assert out["exitCode"] == 0


def test_supplied_cmd_fail_is_completed_not_failed(fake_task_input, tmp_git_repo):
    result = run_checks(fake_task_input(repoPath=str(tmp_git_repo), cmd="false"))
    # Fail-soft: a red suite COMPLETES, it does not FAIL the task.
    assert _completed(result)
    assert result.status.value != "FAILED"
    out = result.output_data
    assert out["passed"] is False
    assert out["ran"] is True
    assert out["exitCode"] == 1


def test_nonzero_exit_code_captured(fake_task_input, tmp_git_repo):
    result = run_checks(fake_task_input(repoPath=str(tmp_git_repo), cmd="exit 7"))
    assert _completed(result)
    out = result.output_data
    assert out["passed"] is False
    assert out["exitCode"] == 7


def test_stdout_stderr_captured_and_capped(fake_task_input, tmp_git_repo):
    result = run_checks(fake_task_input(
        repoPath=str(tmp_git_repo), cmd="echo hello; echo boom 1>&2"))
    assert _completed(result)
    out = result.output_data
    assert "hello" in out["stdout"]
    assert "boom" in out["stderr"]
    assert len(out["stdout"]) <= OUTPUT_CAP + 100  # cap + truncation note


def test_shell_operators_supported(fake_task_input, tmp_git_repo):
    result = run_checks(fake_task_input(
        repoPath=str(tmp_git_repo), cmd="true && echo ok"))
    assert _completed(result)
    assert result.output_data["passed"] is True


def test_empty_cmd_no_detection_is_skip_pass(fake_task_input, tmp_path):
    # A bare dir with no markers => nothing detected => fail-open skip.
    result = run_checks(fake_task_input(repoPath=str(tmp_path), cmd=""))
    assert _completed(result)
    out = result.output_data
    assert out["ran"] is False
    assert out["detected"] is False
    assert out["passed"] is True
    assert out["exitCode"] is None


def test_missing_repopath_fails(fake_task_input):
    result = run_checks(fake_task_input(cmd="true"))
    assert result.status.value == "FAILED"


def test_label_in_summary(fake_task_input, tmp_git_repo):
    result = run_checks(fake_task_input(
        repoPath=str(tmp_git_repo), cmd="true", label="alpha"))
    assert _completed(result)
    summary = result.output_data["summary"]
    assert "run_checks[alpha]" in summary
    assert "pass" in summary


# --- _detect_default micro-tests --------------------------------------------

def _touch(d: Path, name: str, content: str = "") -> None:
    (d / name).write_text(content)


def test_detect_pyproject(tmp_path):
    _touch(tmp_path, "pyproject.toml")
    assert _detect_default(str(tmp_path)) == "pytest -q"


def test_detect_npm_test(tmp_path):
    _touch(tmp_path, "package.json", '{"scripts": {"test": "jest"}}')
    assert _detect_default(str(tmp_path)) == "npm test --silent"


def test_detect_npm_no_test_script(tmp_path):
    _touch(tmp_path, "package.json", '{"scripts": {"build": "tsc"}}')
    assert _detect_default(str(tmp_path)) is None


def test_detect_npm_malformed_json(tmp_path):
    _touch(tmp_path, "package.json", "{ not valid json ")
    assert _detect_default(str(tmp_path)) is None


def test_detect_makefile_test(tmp_path):
    _touch(tmp_path, "Makefile", "build:\n\tgcc\ntest:\n\t./run\n")
    assert _detect_default(str(tmp_path)) == "make test"


def test_detect_makefile_without_test(tmp_path):
    _touch(tmp_path, "Makefile", "build:\n\tgcc\n")
    assert _detect_default(str(tmp_path)) is None


def test_detect_go(tmp_path):
    _touch(tmp_path, "go.mod", "module x\n")
    assert _detect_default(str(tmp_path)) == "go test ./..."


def test_detect_cargo(tmp_path):
    _touch(tmp_path, "Cargo.toml", "[package]\n")
    assert _detect_default(str(tmp_path)) == "cargo test"


def test_detect_empty_dir(tmp_path):
    assert _detect_default(str(tmp_path)) is None


def test_detect_ordering_pyproject_before_go(tmp_path):
    _touch(tmp_path, "pyproject.toml")
    _touch(tmp_path, "go.mod", "module x\n")
    assert _detect_default(str(tmp_path)) == "pytest -q"
