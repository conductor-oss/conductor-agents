# Testing

> Reuses [architecture.md](./architecture.md), [data-model.md](./data-model.md)
> and [workflows.md](./workflows.md) verbatim. Contract (per
> `workers/tests/conftest.py`): pure-logic unit tests — no live Conductor
> server, no network, no real LLM, no `gh`. The only real subprocess allowed is
> `git` (here also `bash`/shell built-ins) against throwaway dirs under
> `tmp_path`.

Two kinds of tests: **unit** for `common.checks` + the `run_checks` worker
(new file), and **workflow-JSON invariants** (extend the existing file).

---

## 1. `workers/tests/test_checks.py` (new)

Mirrors `test_gitops.py`: imports the worker directly, drives it with the
`fake_task_input` fixture, asserts on `output_data`. `common.checks` is also
tested directly (it is pure). Commands use shell built-ins (`true`, `false`,
`echo`) or `python -c …` so no project toolchain is required in CI.

Helpers reused from `conftest.py`: `fake_task_input`, `tmp_path`. A local
`_completed(result)` mirrors `test_gitops.py`.

### 1.1 `common.checks.run_checks` (pure)

| Test | Setup | Assert |
|---|---|---|
| `test_passing_cmd` | `run_checks(tmp, "true")` | `passed`, `exitCode == 0`, `skipped is False`, `detected is False`, `cmd == "true"`. |
| `test_failing_cmd` | `run_checks(tmp, "false")` | `passed is False`, `exitCode == 1`, `skipped is False`. |
| `test_nonzero_does_not_raise` | `run_checks(tmp, "exit 3")` | returns a dict (no `RunError`), `exitCode == 3`, `passed is False`. |
| `test_captures_stdout_stderr` | `run_checks(tmp, "echo out; echo err 1>&2")` | `"out" in stdout`, `"err" in stderr`. |
| `test_runs_in_repo_path` | write `marker.txt` in `tmp`; `run_checks(tmp, "test -f marker.txt")` | `passed is True` (cwd is `repo_path`). |
| `test_stdout_capped` | cmd printing > `STDIO_CAP` chars | `len(stdout) <= STDIO_CAP + slack` (cap notice). |
| `test_skip_when_no_cmd_and_no_default` | empty dir | `skipped is True`, `passed is True`, `cmd == ""`, `exitCode is None`, `detected is False`. |

### 1.2 `detect_default_cmd`

| Test | Setup | Assert |
|---|---|---|
| `test_detect_pytest_pyproject` | write `pyproject.toml` | `== "pytest -q"`. |
| `test_detect_pytest_tests_dir` | mkdir `tests/` | `== "pytest -q"`. |
| `test_detect_npm` | write `package.json` with `{"scripts":{"test":"jest"}}` | `== "npm test"`. |
| `test_detect_npm_ignores_no_test_script` | `package.json` with no `test` script | falls through (not `npm test`). |
| `test_detect_make` | write `Makefile` with a `test:` target | `== "make test"`. |
| `test_detect_go` | write `go.mod` | `== "go test ./..."`. |
| `test_detect_cargo` | write `Cargo.toml` | `== "cargo test"`. |
| `test_detect_none` | empty dir | `is None`. |
| `test_detect_priority` | both `pyproject.toml` and `package.json` | `== "pytest -q"` (priority 1 wins). |
| `test_detect_used_when_no_cmd` | pytest-shaped dir, `run_checks(tmp)` (no cmd) | `detected is True`, `cmd == "pytest -q"`. |

### 1.3 `run_checks` worker wrapper

| Test | Setup | Assert |
|---|---|---|
| `test_worker_pass` | `fake_task_input(repoPath=tmp, cmd="true")` | `_completed(result)`, `output_data["passed"] is True`. |
| `test_worker_fail_is_completed_not_failed` | `cmd="false"` | `_completed(result)` (status `COMPLETED`, **not** `FAILED`), `output_data["passed"] is False`, `exitCode == 1`. |
| `test_worker_skip` | empty dir, no `cmd` | `_completed(result)`, `output_data["skipped"] is True`. |
| `test_worker_missing_repopath_fails` | input with no `repoPath` | status `FAILED` (genuine error path via `fail()`). |

The `test_worker_fail_is_completed_not_failed` case is the crux of the whole
ticket: a red suite must be an *expected outcome* the workflow branches on, not a
worker error.

---

## 2. `workers/tests/test_workflows.py` (extend)

### 2.1 Required change to the existing helper

`_worker_task_names()` scans a fixed module tuple. Add the new module so the
parity invariants (`test_simple_tasks_map_to_registered_worker_tasks`,
`test_taskdefs_map_to_registered_worker_tasks`) see `run_checks`:

```python
for mod in ("coding_agent/tasks.py", "gitops/tasks.py", "test/tasks.py"):
```

Without this, those two existing tests would fail once `run_checks` is added —
the one deliberate edit to the shared invariant file.

### 2.2 New invariants (verification-gate specific)

```python
def test_run_checks_taskdef_registered():
    # taskdefs/run_checks.json exists, is well-formed, retryCount == 0.

def test_code_subtask_has_gated_checks():
    wf = _load(WORKFLOWS_DIR / "code_subtask.json")
    by_ref = {t["taskReferenceName"]: t for t in _collect_tasks(wf)}
    assert by_ref["checks"]["name"] == "run_checks"
    gate = by_ref["verify_gate"]
    assert gate["type"] == "SWITCH"
    # commit lives ONLY inside the "true" branch — never unconditional.
    true_refs = {t["taskReferenceName"] for t in gate["decisionCases"]["true"]}
    assert "cmt" in true_refs
    top_level = [t["taskReferenceName"] for t in wf["tasks"]]
    assert "cmt" not in top_level

def test_code_subtask_passes_testcmd_input():
    wf = _load(WORKFLOWS_DIR / "code_subtask.json")
    assert "testCmd" in wf["inputParameters"]
    checks = next(t for t in _collect_tasks(wf) if t["taskReferenceName"] == "checks")
    assert checks["inputParameters"]["cmd"] == "${workflow.input.testCmd}"

def test_code_parallel_verifies_merged_branch():
    wf = _load(WORKFLOWS_DIR / "code_parallel.json")
    refs = [t["taskReferenceName"] for t in wf["tasks"]]
    # verify runs AFTER merge and BEFORE aggregate.
    assert refs.index("merge") < refs.index("verify") < refs.index("aggregate")
    verify = next(t for t in _collect_tasks(wf) if t["taskReferenceName"] == "verify")
    assert verify["name"] == "run_checks"
    assert wf["outputParameters"]["verified"] == "${verify.output.passed}"

def test_build_forks_passes_testcmd():
    wf = _load(WORKFLOWS_DIR / "code_parallel.json")
    bf = next(t for t in _collect_tasks(wf) if t["taskReferenceName"] == "build_forks")
    assert "testCmd" in bf["inputParameters"]["queryExpression"]

def test_unrelated_workflows_have_no_run_checks():
    for name in ("design_docs", "pr_review", "address_pr", "issue_to_pr", "github_demo"):
        wf = _load(WORKFLOWS_DIR / f"{name}.json")
        names = {t.get("name") for t in _collect_tasks(wf)}
        assert "run_checks" not in names
```

These reuse the file's existing helpers (`_load`, `_collect_tasks`,
`WORKFLOWS_DIR`). The generic invariants already in the file
(`test_every_simple_task_has_a_taskdef`, `test_all_output_references_resolve`,
`test_no_dangling_switch_branches`, `test_task_reference_names_unique_within_workflow`)
automatically cover the new `checks` / `verify` / `verify_gate` nodes — no edit
needed beyond §2.1.

---

## 3. Regression guarantee

- **Existing `test_gitops.py`** is untouched — `merge_worktrees` behaviour is
  unchanged.
- **Existing `test_workflows.py` invariants** stay green given the §2.1 one-line
  module-tuple addition.
- **Backward compatibility**: a plan with no `testCmd` and a repo with no
  detectable suite ⇒ `run_checks` skips ⇒ `passed=true` ⇒ `code_subtask` still
  commits and `code_parallel` still merges, exactly as before this change. The
  gate only ever *subtracts* a broken commit, never blocks healthy work.

---

## 4. How to run

```
cd coding-harness && make test        # or: uv run pytest workers/tests -q
```

Acceptance: `test_checks.py` passes and the full `workers/tests` suite stays
green.
