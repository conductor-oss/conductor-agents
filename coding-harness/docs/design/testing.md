# Testing — `run_checks` unit tests & workflow invariants

> Supporting doc for [architecture.md](./architecture.md) (CCOR-13227). Reuses its
> names and the `run_checks` contract (§4) verbatim. If anything diverges,
> architecture.md wins.

Contract (from `workers/tests/conftest.py`): pure-logic unit tests — **no** live
Conductor server, **no** network, **no** LLM, **no** `gh`. The only real subprocess
allowed is `git` (and, for `run_checks`, `bash`) against throwaway repos under
`tmp_path`. Fixtures reused: `fake_task_input`, `tmp_git_repo`.

---

## 1. New file — `workers/tests/test_checks.py`

Mirrors `test_gitops.py` in style: imports the worker bare (`from checks.tasks import
run_checks`), invokes it with a `fake_task_input(...)` `Task`, and asserts on
`result.status.value` and `result.output_data`. A local `_completed(result)` helper
(same as `test_gitops.py`) checks `result.status.value == "COMPLETED"`.

`run_checks` runs a **real** shell command via `bash -c`, so tests use trivially
deterministic commands (`true`, `false`, `exit 7`, `echo …`) — fast, no toolchain
needed. `tmp_git_repo` supplies a valid `repoPath`.

### Cases

| Test | Setup | Asserts |
|---|---|---|
| `test_supplied_cmd_pass` | `cmd="true"` | COMPLETED; `passed True`, `ran True`, `detected False`, `exitCode 0`. |
| `test_supplied_cmd_fail_is_completed_not_failed` | `cmd="false"` | COMPLETED (**not** FAILED — fail-soft); `passed False`, `ran True`, `exitCode 1`. |
| `test_nonzero_exit_code_captured` | `cmd="exit 7"` | `passed False`, `exitCode 7`. |
| `test_stdout_stderr_captured_and_capped` | `cmd="echo hello; echo boom 1>&2"` | `stdout` contains `hello`, `stderr` contains `boom`; both `len <= OUTPUT_CAP` (+ truncation note). |
| `test_shell_operators_supported` | `cmd="true && echo ok"` | `passed True` (proves `bash -c`, not bare-exec). |
| `test_empty_cmd_detects_pytest` | write `pyproject.toml` into repo, `cmd=""` | `detected True`, `cmd == "pytest -q"`, `ran True`. |
| `test_empty_cmd_no_detection_is_skip_pass` | bare dir, no markers, `cmd=""` | `ran False`, `detected False`, `passed True` (fail-open), `exitCode None`. |
| `test_missing_repopath_fails` | no `repoPath` | FAILED (`result.status.value == "FAILED"`); genuine worker error. |
| `test_label_in_summary` | `cmd="true", label="alpha"` | `summary` contains `run_checks[alpha]` and `pass`. |

> Detection tests assert `_detect_default` **shape only** (that it returns the mapped
> command); they do **not** run `pytest`/`npm`/etc. `test_empty_cmd_detects_pytest`
> supplies `cmd=""` with a `pyproject.toml` present and asserts `cmd == "pytest -q"`,
> but because that command *does* run against the throwaway repo, either assert on
> `detected`/`cmd` regardless of exit, or unit-test `_detect_default(str(repo))`
> directly (import it from `checks.tasks`) to avoid depending on pytest's own exit.

### `_detect_default` unit micro-tests

Import `_detect_default` directly and assert the §4 table in
[data-model.md](./data-model.md): `pyproject.toml`→`pytest -q`; `package.json` with a
`scripts.test`→`npm test --silent`; `Makefile` with `test:`→`make test`;
`go.mod`→`go test ./...`; `Cargo.toml`→`cargo test`; empty dir→`None`; ordering
(pyproject wins over a co-present `go.mod`).

---

## 2. Edits to `workers/tests/test_workflows.py`

`_worker_task_names()` currently scans only `coding_agent/tasks.py` and
`gitops/tasks.py`. **Add the new module** so the parity invariants still cover
`run_checks`:

```python
for mod in ("coding_agent/tasks.py", "gitops/tasks.py", "checks/tasks.py"):
```

Without this, `test_simple_tasks_map_to_registered_worker_tasks` and
`test_taskdefs_map_to_registered_worker_tasks` would fail once `run_checks` appears in
a workflow / taskdef. With it, they pass. The existing generic invariants already
cover the rest of the wiring automatically:

- `test_json_is_well_formed` — validates the new `run_checks.json` and edited workflows.
- `test_every_simple_task_has_a_taskdef` — `run_checks` ↔ `taskdefs/run_checks.json`.
- `test_all_output_references_resolve` — new `${check.…}` / `${verify.…}` refs resolve.
- `test_no_dangling_switch_branches` — `verify_gate`'s `"true"` case is non-empty; empty
  `defaultCase` allowed.
- `test_task_reference_names_unique_within_workflow` — `check`, `verify_gate`, `cmt`,
  `verify` unique.

### New focused invariants (append to `test_workflows.py`)

Small, JSON-only assertions that lock the *gate shape* so a future edit can't silently
un-wire it:

```python
def test_code_subtask_gates_commit_behind_run_checks():
    """commit runs only inside verify_gate's true case, downstream of run_checks."""
    wf = _load(WORKFLOWS_DIR / "code_subtask.json")
    tasks = _collect_tasks(wf)
    by_ref = {t["taskReferenceName"]: t for t in tasks if "taskReferenceName" in t}
    assert by_ref["check"]["name"] == "run_checks"
    gate = by_ref["verify_gate"]
    assert gate["type"] == "SWITCH"
    true_case = {t["taskReferenceName"] for t in gate["decisionCases"]["true"]}
    assert "cmt" in true_case                       # commit is gated
    assert gate["decisionCases"]["true"][0]["name"] == "commit"
    # commit must NOT also exist as a top-level (ungated) task.
    top = {t["taskReferenceName"] for t in wf["tasks"]}
    assert "cmt" not in top

def test_code_parallel_verifies_after_merge_before_aggregate():
    wf = _load(WORKFLOWS_DIR / "code_parallel.json")
    order = [t["taskReferenceName"] for t in wf["tasks"]]
    assert order.index("merge") < order.index("verify") < order.index("aggregate")
    verify = next(t for t in wf["tasks"] if t["taskReferenceName"] == "verify")
    assert verify["name"] == "run_checks"
    assert wf["outputParameters"]["verified"] == "${verify.output.passed}"

def test_run_checks_only_in_code_workflows():
    """run_checks must not leak into the unrelated workflows."""
    for name in ("design_docs", "pr_review", "address_pr", "issue_to_pr", "github_demo"):
        names = {o.get("name") for o in _iter_objects(_load(WORKFLOWS_DIR / f"{name}.json"))}
        assert "run_checks" not in names
```

---

## 3. Regression guard — "no change to unrelated workflows"

`test_run_checks_only_in_code_workflows` (above) plus the untouched files on disk cover
the acceptance criterion *"No change to unrelated workflows (design_docs, pr_review,
address_pr, issue_to_pr)."* Those five JSON files are not edited by this change.

---

## 4. How to run

Same as today (no new deps):

```bash
cd coding-harness/workers
pytest -q                      # full suite: new test_checks.py + edited test_workflows.py
pytest -q tests/test_checks.py # just the new worker tests
```

Acceptance mapping: `test_supplied_cmd_fail_is_completed_not_failed` +
`test_code_subtask_gates_commit_behind_run_checks` prove a failing sub-task check does
not commit; `test_code_parallel_verifies_after_merge_before_aggregate` proves the
integration gate is wired and surfaced; the whole suite staying green proves *"existing
`pytest tests/` stays green."*
