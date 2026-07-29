"""Executes code_parallel.json's verify_loop jq queryExpressions against
representative sample input, so a bad edit to a query string is caught. Locks
in:

- build_checks must take checksList from a per-iteration re-parse of tasks.md
  (reparse_tasks), not build_forks's plan-time snapshot, so a fixup agent's
  edit to a Test: line is actually picked up on the next round.
- verify_round must fold `openspec validate`'s result (validate_spec) into the
  same passed/notPassed gate as the real test commands and the semantic judge,
  so a broken proposal/design/specs/tasks.md is fixed by the loop's fixup pass
  instead of shipping an inconsistent change.
- verify_loop never attempts to archive the OpenSpec change: the loop body
  runs unconditionally every iteration (no archive-already-done gate), and
  `passed` depends only on checks/judge/specValid — never on an archive
  outcome. Archiving is out of scope for this workflow; see design.md.

Skips if the system has no ``jq`` binary — mirrors test_openspec.py's
pinned-CLI skip.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WF = Path(__file__).resolve().parents[1] / "workflows"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq binary not installed")


def _load(name: str) -> dict:
    return json.loads((WF / f"{name}.json").read_text())


def _search(nodes, ref):
    """Return the task dict for ``ref`` anywhere in ``nodes`` (recursing into
    DO_WHILE loopOver and SWITCH decisionCases/defaultCase), or None."""
    for node in nodes:
        if node.get("taskReferenceName") == ref:
            return node
        if node.get("type") == "DO_WHILE":
            found = _search(node.get("loopOver") or [], ref)
            if found is not None:
                return found
        if node.get("type") == "SWITCH":
            for branch in list((node.get("decisionCases") or {}).values()) + [node.get("defaultCase") or []]:
                found = _search(branch, ref)
                if found is not None:
                    return found
    return None


def _find(nodes, ref):
    found = _search(nodes, ref)
    if found is None:
        raise AssertionError(f"task {ref!r} not found")
    return found


def _eval_jq(query: str, data: dict, tmp_path: Path) -> dict:
    prog = tmp_path / "prog.jq"
    prog.write_text(query, encoding="utf-8")
    proc = subprocess.run(["jq", "-f", str(prog)], input=json.dumps(data),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_build_checks_reads_from_reparse_tasks_not_build_forks():
    """The exact bug: checksList must come from the live per-iteration reparse
    (reparse_tasks), not from build_forks's plan-time snapshot — otherwise a
    fixup agent's edit to a Test: line is never actually re-checked."""
    workflow = _load("code_parallel")
    build_checks = _find(workflow["tasks"], "build_checks")
    assert build_checks["inputParameters"]["checksList"] == "${reparse_tasks.output.subtasks}"
    reparse = _find(workflow["tasks"], "reparse_tasks")
    assert reparse["name"] == "openspec_tasks_to_subtasks"
    assert reparse["inputParameters"]["tasksPath"] == "${openspec_plan.output.changeDir}/tasks.md"


def test_build_checks_query_builds_fork_input_and_allowlist(tmp_path):
    workflow = _load("code_parallel")
    query = _find(workflow["tasks"], "build_checks")["inputParameters"]["queryExpression"]
    result = _eval_jq(query, {
        "repoPath": "/tmp/repo",
        "checksList": [
            {"id": "setup", "testCmd": "python3 hello.py"},
            {"id": "build", "testCmd": "make check"},
        ],
    }, tmp_path)
    assert {t["taskReferenceName"] for t in result["dynamicTasks"]} == {"check_setup", "check_build"}
    assert result["dynamicTasksInput"]["check_setup"]["testCmd"] == "python3 hello.py"
    assert "Bash(make *)" in result["fixupAllowedTools"]
    assert "Bash(python3 *)" in result["fixupAllowedTools"]  # already in DEFAULT_ALLOWED_TOOLS


def test_build_checks_reflects_a_fixed_up_test_command(tmp_path):
    """Simulates round 2 after a fixup agent rewrote `Test: python hello.py` to
    `Test: python3 hello.py` in tasks.md: the re-parsed checksList must produce
    a different check command, not the original `python` one."""
    workflow = _load("code_parallel")
    query = _find(workflow["tasks"], "build_checks")["inputParameters"]["queryExpression"]
    before = _eval_jq(query, {
        "repoPath": "/tmp/repo",
        "checksList": [{"id": "setup", "testCmd": "python hello.py"}],
    }, tmp_path)
    after = _eval_jq(query, {
        "repoPath": "/tmp/repo",
        "checksList": [{"id": "setup", "testCmd": "python3 hello.py"}],
    }, tmp_path)
    assert before["dynamicTasksInput"]["check_setup"]["testCmd"] == "python hello.py"
    assert after["dynamicTasksInput"]["check_setup"]["testCmd"] == "python3 hello.py"


def test_verify_round_folds_checks_judge_and_openspec_validate(tmp_path):
    """verify_round is the loop's single pass/fail gate: it must require the
    real test commands, the semantic judge, AND `openspec validate` to all
    pass — a broken proposal/design/specs/tasks.md must keep `passed` false
    and surface as a fixable finding, with no separate archive-dependent
    stage."""
    workflow = _load("code_parallel")
    query = _find(workflow["tasks"], "verify_round")["inputParameters"]["queryExpression"]

    all_good = _eval_jq(query, {
        "checks": {"allPassed": True}, "judge": {"passed": True, "findings": []},
        "specValid": True, "specIssues": [],
    }, tmp_path)
    assert all_good["passed"] is True and all_good["notPassed"] is False

    bad_spec = _eval_jq(query, {
        "checks": {"allPassed": True}, "judge": {"passed": True, "findings": []},
        "specValid": False, "specIssues": [{"message": "missing Scenario for Requirement X"}],
    }, tmp_path)
    assert bad_spec["passed"] is False and bad_spec["notPassed"] is True
    assert any("OpenSpec validate failed" in f and "missing Scenario for Requirement X" in f
              for f in bad_spec["findings"])

    failing_checks = _eval_jq(query, {
        "checks": {"allPassed": False}, "judge": {"passed": True, "findings": ["some judge finding"]},
        "specValid": True, "specIssues": [],
    }, tmp_path)
    assert failing_checks["passed"] is False
    assert failing_checks["findings"] == ["some judge finding"]


def test_validate_spec_task_is_wired_into_the_verify_loop_before_the_gate():
    workflow = _load("code_parallel")
    validate_spec = _find(workflow["tasks"], "validate_spec")
    assert validate_spec["name"] == "openspec_validate_change"
    assert validate_spec["inputParameters"]["changeId"] == "${openspec_plan.output.changeName}"
    verify_round = _find(workflow["tasks"], "verify_round")
    assert verify_round["inputParameters"]["specValid"] == "${validate_spec.output.valid}"


def test_verify_loop_never_archives():
    """code_parallel does not archive the OpenSpec change: no archive-related
    task, gate, or workflow variable exists anywhere in verify_loop or in
    outputParameters. Archiving is a manual, out-of-band step a human performs
    after the PR merges — see design.md's Decision 6."""
    workflow = _load("code_parallel")
    verify_loop = next(t for t in workflow["tasks"] if t["taskReferenceName"] == "verify_loop")
    top_level_refs = [t["taskReferenceName"] for t in verify_loop["loopOver"]]
    assert top_level_refs == [
        "reparse_tasks", "build_checks", "checks_fan_out", "checks_join", "checks_summary",
        "validate_spec", "verify_judge", "verify_round", "verify_round_state", "verify_action",
    ], "the loop body must run unconditionally — no archive-already-done gate wrapping it"
    dumped = json.dumps(workflow)
    assert "archive" not in dumped.lower(), "no archive step, variable, or output should remain in code_parallel.json"
