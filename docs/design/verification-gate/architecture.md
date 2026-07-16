# Architecture — Pre-merge verification gate (`run_checks`)

> **Ticket:** CCOR-13227 (parent CCOR-13227) — *Add an automated test/verification
> gate before merge in `coding-harness`.*
>
> **This document is the single source of truth.** The supporting docs
> — [data-model.md](./data-model.md), [workflows.md](./workflows.md),
> [testing.md](./testing.md) — reuse the names, types, file layout, and
> task/reference names defined here **verbatim**. If anything conflicts, this
> file wins.

---

## 1. Overview

`coding-harness`'s parallel-implementation workflow
(`workers/workflows/code_parallel.json`) decomposes one instruction into
independent sub-tasks, runs each in its own git worktree via
`code_subtask.json` (`worktree_add → coding_agent → commit`), then merges every
branch with `merge_worktrees` and reports `merged` / `conflicts`. **Nothing ever
runs the project's tests or build.** Success is reported purely on the coding
agent's own say-so.

The planner already asks the decomposition agent for a `testCmd` per sub-task
(`code_parallel.json` → `plan` task schema) and `build_forks` already
interpolates it into each sub-task prompt as `"Your check: …"` text — but nothing
executes it. It is decorative.

This change adds a single new worker task, **`run_checks`**, and wires it into
the two workflows that produce merges:

1. **`code_subtask`** — after `coding_agent`, *before* `commit`: run the
   sub-task's own `testCmd`. If it fails, the sub-task is **flagged and not
   committed** (so broken work never reaches `merge_worktrees`).
2. **`code_parallel`** — after `merge_worktrees`, *before* `aggregate`: run the
   repo's full test/build command against the integrated change branch and
   surface a real pass/fail in the workflow output.

`run_checks` is a thin `@worker_task` over the existing `common/exec.py`
subprocess helper (stdin closed, output captured). Its *pure logic* lives in a
new `common/checks.py`, mirroring how `gitops/tasks.py` is a thin wrapper over
`common/git.py`.

### Design principles (reused verbatim by every component)

- **A failing check is an expected outcome, not an error.** Per
  `common/results.py`, `run_checks` returns a `COMPLETED` `TaskResult` with
  `passed=false` in `output_data`; it does **not** raise or return `fail()` for a
  red test. `fail()` is reserved for genuine worker errors (e.g. `repoPath` does
  not exist). The *workflow* decides what to do with `passed`.
- **No check available ⇒ do not block.** If no command is supplied and none can
  be detected, `run_checks` returns `skipped=true, passed=true`. This keeps
  behaviour backward-compatible for plans that omit `testCmd` and repos with no
  detectable suite — verification is *added*, never *imposed*.
- **The gate lives in the workflow JSON, not the worker.** `code_subtask`
  branches on `${checks.output.passed}` with a `SWITCH`, exactly mirroring the
  existing `design_gate` switch pattern. The worker stays pure and reusable.
- **Untrusted input is run through a shell deliberately and captured.** A
  `testCmd` such as `"npm test && npm run build"` needs shell semantics, so
  `run_checks` executes `["bash", "-c", cmd]` with `check=False` and reads the
  exit code — it never lets a non-zero exit raise.

---

## 2. Tech stack

Unchanged from the rest of `coding-harness/workers`:

- **Python 3** worker package under `coding-harness/workers/`, managed with `uv`.
- **Conductor Python SDK** (`conductor.client.worker.worker_task`) — the
  `@worker_task(task_definition_name=…)` decorator registers pollers.
- **`common/exec.py`** — the *only* subprocess primitive `run_checks` may use.
- **`common/results.py`** — `ok()` / `fail()` / `cap()` `TaskResult` helpers.
- **pytest** for unit + JSON-invariant tests under `workers/tests/`.
- Workflow/task definitions are plain JSON registered by `workers/register.sh`.

No new third-party dependencies.

---

## 3. Complete module / file layout

Everything is under `coding-harness/`. **Create** and **modify** lists are exact.

### Files to CREATE

| Path | Responsibility |
|---|---|
| `workers/common/checks.py` | Pure logic. `detect_default_cmd(repo_path)` and `run_checks(repo_path, cmd)` → the output dict in [data-model.md](./data-model.md). Uses `common.exec.run` and `common.results.cap`. No Conductor imports. |
| `workers/test/__init__.py` | Marks `test` as the worker package that carries the verification task. Empty (matches `gitops/__init__.py`). |
| `workers/test/tasks.py` | The `@worker_task(task_definition_name="run_checks")` wrapper. Reads `task.input_data`, calls `common.checks.run_checks`, returns `ok()` / `fail()`. Thin, exactly like `gitops/tasks.py`. |
| `workers/workflows/taskdefs/run_checks.json` | Conductor task definition for `run_checks`. `retryCount: 0`. |
| `workers/tests/test_checks.py` | Unit tests for `common.checks` + the `run_checks` worker, mirroring `test_gitops.py` style (real subprocess against throwaway repos, no LLM/network). |

### Files to MODIFY

| Path | Change |
|---|---|
| `workers/workflows/code_subtask.json` | Add `testCmd` input; insert `run_checks` (ref `checks`) after `coding_agent`; wrap `commit` in a `verify_gate` `SWITCH` on `${checks.output.passed}`; add verification fields to `outputParameters`. |
| `workers/workflows/code_parallel.json` | `build_forks` JQ passes `testCmd` into each `dynamicTasksInput` entry; add `verifyCmd` workflow input; insert `run_checks` (ref `verify`) after `merge`, before `aggregate`; surface `verified` / `verification` in `outputParameters`; carry `verified` per-subtask in the `aggregate` JQ. |
| `workers/main.py` | `DEFAULT_MODULES = "coding_agent,gitops,test"` so the new package's poller loads by default. |
| `workers/tests/test_workflows.py` | Add `"test/tasks.py"` to `_worker_task_names()`'s module tuple; add invariant tests asserting the two gates are wired and unrelated workflows are untouched. |

### Untouched (acceptance criterion)

`workers/workflows/design_docs.json`, `pr_review.json`, `address_pr.json`,
`issue_to_pr.json`, `github_demo.json` and all their taskdefs are **not**
modified. `common/exec.py`, `common/git.py`, `common/results.py`,
`gitops/tasks.py`, `coding_agent/tasks.py` are **not** modified.

---

## 4. Shared contracts (reuse verbatim)

### 4.1 Task definition name

```
run_checks
```

Used identically as: the `@worker_task(task_definition_name="run_checks")`
decorator argument, the `name` field in `taskdefs/run_checks.json`, and the
`"name": "run_checks"` of every `SIMPLE` task node in the workflows.

### 4.2 Worker input (read from `task.input_data`)

| Key | Type | Required | Meaning |
|---|---|---|---|
| `repoPath` | `str` | **yes** | Working directory the command runs in (a repo root or a worktree path). |
| `cmd` | `str` | no | Shell command to run. Empty / missing ⇒ fall back to `detect_default_cmd(repoPath)`. |

There are no other worker inputs. (In the workflows, `code_subtask` maps its
`testCmd` and `code_parallel` maps its `verifyCmd` onto this `cmd` key — see
[workflows.md](./workflows.md).)

### 4.3 Worker output (`output_data` of the `COMPLETED` result)

The exact dict returned by `common.checks.run_checks` and set as `output_data`.
Field names are canonical — every consumer references them verbatim:

```jsonc
{
  "passed":   true,          // bool: check passed OR was skipped
  "skipped":  false,         // bool: true when no cmd ran (nothing to verify)
  "detected": false,         // bool: true when cmd came from detect_default_cmd
  "cmd":      "pytest -q",   // str: the resolved command actually run ("" if skipped)
  "exitCode": 0,             // int|null: process exit code (null if skipped)
  "stdout":   "…",           // str: captured stdout, capped via results.cap
  "stderr":   ""             // str: captured stderr, capped via results.cap
}
```

Invariants (asserted in tests):

- `skipped == true` ⇒ `passed == true`, `cmd == ""`, `exitCode == null`.
- a command ran ⇒ `passed == (exitCode == 0)`.
- `detected == true` ⇒ a command ran and `cmd` came from detection.

### 4.4 `common/checks.py` public surface

```python
STDIO_CAP = 4000  # chars; passed to results.cap for stdout/stderr

def detect_default_cmd(repo_path: str) -> str | None:
    """Best-effort default check for a repo, or None if none is obvious.
    Deliberately small (broader detection is out of scope, per the ticket):
        pyproject.toml / pytest.ini / tests/  -> "pytest -q"
        package.json with a "test" script     -> "npm test"
        Makefile with a "test:" target        -> "make test"
        go.mod                                 -> "go test ./..."
        Cargo.toml                             -> "cargo test"
    """

def run_checks(repo_path: str, cmd: str | None = None) -> dict:
    """Resolve cmd (arg -> detect_default_cmd -> skip), run it via
    common.exec.run(["bash", "-c", cmd], cwd=repo_path, check=False), and return
    the §4.3 output dict. Never raises for a non-zero exit."""
```

`run_checks` **must** call `common.exec.run` with `check=False` so a red suite
returns a `RunResult` (never a `RunError`). `stdout`/`stderr` are passed through
`common.results.cap(…, STDIO_CAP)`.

### 4.5 `workers/test/tasks.py` shape (verbatim skeleton)

```python
from conductor.client.worker.worker_task import worker_task
from common import checks
from common.results import fail, ok

@worker_task(task_definition_name="run_checks")
def run_checks(task):
    """Run a project's test/build command as a pre-merge gate. Input: repoPath,
    cmd? (falls back to a repo-detected default). A failing check is COMPLETED
    with passed=false — an expected outcome, not a worker error."""
    i = task.input_data or {}
    try:
        out = checks.run_checks(i["repoPath"], i.get("cmd") or None)
        state = "skipped" if out["skipped"] else ("pass" if out["passed"] else "FAIL")
        return ok(task, out, [f"[run_checks] {state} cmd={out['cmd']!r} exit={out['exitCode']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "run_checks", e)
```

### 4.6 Workflow task-reference names (canonical)

| Workflow | New/changed ref | Type | Purpose |
|---|---|---|---|
| `code_subtask` | `checks` | `SIMPLE` `run_checks` | Run the sub-task's `testCmd`. |
| `code_subtask` | `verify_gate` | `SWITCH` | `value-param` on `passed`; case `"true"` → `commit`. |
| `code_parallel` | `verify` | `SIMPLE` `run_checks` | Run the repo command on the merged branch. |

`code_subtask`'s existing refs stay `wt` (`worktree_add`), `code`
(`coding_agent`), `cmt` (`commit`). `commit` moves *inside* the `verify_gate`
`"true"` branch — its ref name `cmt` is unchanged.

### 4.7 New workflow inputs

| Workflow | Input | Default | Meaning |
|---|---|---|---|
| `code_subtask` | `testCmd` | `""` | Per-sub-task check, from the planner's structured output. Maps to `run_checks.cmd`. |
| `code_parallel` | `verifyCmd` | `""` | Full-repo test/build for the integrated branch. Maps to `run_checks.cmd`; empty ⇒ detection. |

### 4.8 Naming conventions

- Worker/task-def name: `snake_case` → `run_checks`.
- Workflow output & worker `output_data` keys: `camelCase` (`passed`,
  `exitCode`, `verifyCmd`, `testCmd`) — matches existing `filesChanged`,
  `costUsd`, `tokenUsed`.
- Task reference names: short lowercase (`checks`, `verify`, `verify_gate`).
- Python module/function: `run_checks`; pure helpers in `common/checks.py`.

---

## 5. End-to-end flow (after this change)

```
code_parallel
  prep → branch → design_gate → plan(→ testCmd per subtask) → build_forks
    → fan_out (FORK_JOIN_DYNAMIC of code_subtask, one per subtask)
    │     code_subtask:
    │       wt(worktree_add) → code(coding_agent) → checks(run_checks, cmd=testCmd)
    │         → verify_gate(SWITCH on passed)
    │              true    → cmt(commit)        # only committed if the check passed
    │              default → (no commit)        # broken work is flagged, never merged
    → fan_join → merge(merge_worktrees)
    → verify(run_checks, cmd=verifyCmd)         # real gate on the integrated branch
    → aggregate
  output: { merged, conflicts, verified, verification, subtasks[], … }
```

**Acceptance mapping**

- *A sub-task whose edits fail its own `testCmd` is flagged/failed rather than
  silently committed* → `checks` runs `testCmd`; `verify_gate` routes a failing
  sub-task away from `commit`; the branch has no new commit, so `merge_worktrees`
  merges nothing broken. The failure is visible in the sub-task output.
- *`code_parallel`'s output includes a real pass/fail for the integrated branch*
  → `verify` output surfaces as `verified` / `verification`.
- *New unit tests pass; existing `pytest tests/` stays green* → see
  [testing.md](./testing.md).
- *No change to unrelated workflows* → §3 "Untouched".

---

## 6. Operational notes

- **Poller registration.** `run_checks` polls only if its module is loaded.
  `main.py`'s `DEFAULT_MODULES` becomes `"coding_agent,gitops,test"`. Hosts that
  override `WORKER_MODULES` must include `test`.
- **Timeouts.** Test/build commands can be long; `run_checks.json` uses
  `timeoutSeconds: 0` + a long `responseTimeoutSeconds` with
  `timeoutPolicy: "ALERT_ONLY"`, matching `merge_worktrees.json`. Runtime
  deadlines remain owned by the task def, per `common/exec.py`'s contract.
- **No retries.** `run_checks.json` sets `retryCount: 0`: re-running a
  deterministic failing suite wastes time and would not change the verdict.
- **Follow-ups (out of scope).** Review/scoring loop, model-ladder escalation,
  `keep_best`, and richer multi-language detection all depend on this
  trustworthy signal existing first and are deferred to separate tickets.
  `docs/CODING_AGENT_WORKER.md`'s "does NOT own" table may later be updated to
  note that the `run_checks` gate now exists.
