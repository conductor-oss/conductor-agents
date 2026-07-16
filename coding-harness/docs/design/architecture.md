# Architecture — Pre-merge verification gate (`run_checks`)

> **Ticket:** CCOR-13227 — Add an automated test/verification gate before merge in
> `coding-harness`. **Parent:** CCOR-13227.
>
> **This document is the single source of truth.** The supporting docs
> ([data-model.md](./data-model.md), [workflow-wiring.md](./workflow-wiring.md),
> [testing.md](./testing.md)) reuse the names, types, file layout, task-reference
> names, and JSON shapes defined here **verbatim**. If a supporting doc appears to
> disagree with this file, **this file wins**.

---

## 1. Overview

`coding-harness`'s parallel-implementation workflow
(`workers/workflows/code_parallel.json`) decomposes an instruction into independent
sub-tasks, runs each in its own git worktree via `code_subtask.json`
(`worktree_add → coding_agent → commit`), then merges every branch with
`merge_worktrees` and reports `merged` / `conflicts`. **Nothing runs the project's
tests or build.** Success is reported on the coding agent's own say-so, and the
planner's per-sub-task `testCmd` is interpolated into the coding prompt as decorative
`"Your check: …"` text that is **never executed**
(`workers/workflows/code_parallel.json:141`, `:152`).

This change adds **one** new worker task — **`run_checks`** — and wires it into the
two workflows that produce code so that:

1. **Sub-task gate.** A sub-task's own `testCmd` runs **after `coding_agent`, before
   `commit`**. If it fails, the sub-task is **not committed** (its edits never reach
   the change branch) and the sub-task result is flagged `verified: false`.
2. **Integration gate.** The **merged** change branch is verified **after
   `merge_worktrees`, before `aggregate`**, and a real pass/fail result is surfaced in
   `code_parallel`'s workflow output — not just `merged` / `conflicts`.

`run_checks` is the concrete implementation of the `test / run_checks` worker that
`docs/CODING_AGENT_WORKER.md`'s "does NOT own" table already names as the home for
"Running the project's test suite as a gate" — a worker that **does not exist today**.
It also directly addresses `workers/IMPROVEMENTS.md`'s top priority ("don't report
success that wasn't earned"); the implementation here is designed against *this*
codebase and does **not** follow IMPROVEMENTS.md's own sketch.

### Goals

- One small, reusable, **fail-soft**, LLM-free, network-free worker that runs a shell
  command in a repo and reports a **trustworthy** pass/fail via the existing
  `common/exec.py` subprocess helper (stdin closed, output captured).
- A sub-task whose edits fail its check is flagged and its work is **not merged**.
- `code_parallel` surfaces an **independent** verification signal a caller (or a future
  review/scoring loop) can trust.

### Non-goals (explicitly out of scope — follow-up tickets)

- Review/scoring loop, model-ladder escalation, `keep_best` (they depend on
  trustworthy verification existing first — i.e. on this ticket).
- Rich multi-language `testCmd` detection beyond the minimal best-effort fallback in
  §5.3. The **primary** source of a command is the planner's `testCmd` / the caller's
  `verifyCmd`; detection is only a convenience default.
- Any change to the coding agent, git/merge behaviour, or the unrelated workflows
  `design_docs`, `pr_review`, `address_pr`, `issue_to_pr`.

---

## 2. Tech stack

Unchanged from the rest of `coding-harness/workers`:

- **Python 3**, Conductor Python SDK (`conductor.client.worker.worker_task`).
- Worker tasks return a `TaskResult` built by `common/results.py` (`ok()` / `fail()`).
- Subprocess execution via `common/exec.py` (`run()`, `RunResult`, `RunError`).
- Orchestration in Conductor **workflow JSON** (`SIMPLE`, `SWITCH`,
  `FORK_JOIN_DYNAMIC`, `JSON_JQ_TRANSFORM`).
- Tests: **pytest**, pure-logic, no live Conductor / network / LLM
  (`workers/tests/`), using the real `git` binary against throwaway repos.

No new third-party dependencies. Shell commands are executed through `bash -c` (see
§4) so a `testCmd` may contain pipes / `&&`.

---

## 3. Module / file layout

Everything lives under `coding-harness/`. **Bold** = new file; the rest are edits to
existing files.

| Path | Status | Responsibility |
|---|---|---|
| **`workers/checks/__init__.py`** | new | Empty package marker for the new `checks` worker module. |
| **`workers/checks/tasks.py`** | new | Defines `@worker_task(task_definition_name="run_checks")` — the `run_checks` worker + the private `_detect_default()` helper. |
| **`workers/workflows/taskdefs/run_checks.json`** | new | Conductor task definition for the `run_checks` SIMPLE task. |
| **`workers/tests/test_checks.py`** | new | Unit tests for `run_checks` (mirrors `test_gitops.py` style: real subprocess against `tmp_git_repo`, no LLM/network). |
| `workers/workflows/code_subtask.json` | edit | Add `testCmd` input; insert `run_checks` (`check`) after `coding_agent`; gate `commit` behind a `SWITCH` (`verify_gate`) on `check.output.passed`; add verification fields to output. |
| `workers/workflows/code_parallel.json` | edit | Add `verifyCmd` input; thread `testCmd` into `build_forks`' dynamic sub-task inputs; insert `run_checks` (`verify`) after `merge_worktrees`, before `aggregate`; surface verification in workflow output + `aggregate` summary. |
| `workers/main.py` | edit | Add `checks` to `DEFAULT_MODULES` so the poller registers the new worker. |
| `workers/tests/test_workflows.py` | edit | Add `checks/tasks.py` to `_worker_task_names()`; add invariants for the new gate (see [testing.md](./testing.md)). |

### Naming conventions (reused verbatim everywhere)

| Kind | Name | Notes |
|---|---|---|
| Worker package / import module | `checks` | Directory `workers/checks/`. **Not** `test` — that name collides with CPython's stdlib `test` package and with the existing `workers/tests/` dir. This is the "or alongside gitops" option the ticket allows. |
| Worker function | `run_checks(task)` | in `workers/checks/tasks.py`. |
| Task definition name / SIMPLE `name` | `run_checks` | one string; used in the taskdef, both workflows, `register.sh` (auto), and the parity tests. |
| Detection helper | `_detect_default(repo_path: str) -> str \| None` | private module function. |
| Sub-task check ref (in `code_subtask`) | `check` | `taskReferenceName`. |
| Sub-task commit gate ref | `verify_gate` | the `SWITCH`. |
| Integration check ref (in `code_parallel`) | `verify` | `taskReferenceName`. |
| `code_subtask` input | `testCmd` | per-sub-task command from the planner. |
| `code_parallel` input | `verifyCmd` | repo-wide integration command from the caller. |

---

## 4. Shared contract — the `run_checks` worker

**One** worker, used by **both** gates. Its input and output shape is the single
contract every consumer reuses. (Full field-by-field types, capping constants, and the
detection table are in [data-model.md](./data-model.md); the exact workflow JSON is in
[workflow-wiring.md](./workflow-wiring.md).)

### Input (`task.input_data`)

| Field | Type | Required | Meaning |
|---|---|---|---|
| `repoPath` | `string` | **yes** | Working directory to run the check in (a worktree path for the sub-task gate; the repo root for the integration gate). |
| `cmd` | `string` | no | Shell command to run. Empty/absent → fall back to `_detect_default(repoPath)`. |
| `label` | `string` | no | Free-text tag for log lines (e.g. the sub-task id, or `"integration"`). |

### Output (`task.output_data`) — the shape both gates read

```json
{
  "cmd":        "pytest -q",   // command actually used ("" if nothing to run)
  "ran":        true,          // did a command actually execute?
  "detected":   false,         // did `cmd` come from _detect_default (vs supplied)?
  "passed":     true,          // see semantics below
  "exitCode":   0,             // process exit code; null when ran == false
  "stdout":     "…",          // capped to OUTPUT_CAP chars
  "stderr":     "…",          // capped to OUTPUT_CAP chars
  "durationMs": 1234,
  "summary":    "run_checks[integration]: pass (exit 0) cmd=`pytest -q`"
}
```

**`passed` semantics — fail-*open* on "nothing to run", fail-*closed* on a real
failure:**

```
passed = (exitCode == 0)   when a command ran
passed = true              when nothing ran (no cmd supplied and none detected)
```

Rationale: a real command that exits non-zero is the signal we must **not** hide, so
`passed` is `false` and the sub-task gate blocks. But "the planner gave no `testCmd`
and we couldn't detect one" must **not** be worse than today's behaviour — we commit
anyway (`passed = true`, but `ran = false` records the truth). Consumers that need to
distinguish "green because it passed" from "green because nothing ran" read `ran`.

### Execution & error posture

- Runs `common.exec.run(["bash", "-c", cmd], cwd=repoPath, check=False)`. `bash -c`
  lets a `testCmd` contain pipes / `&&`; `check=False` means a non-zero exit returns a
  `RunResult` (it is **not** an exception) — a failing test is an **expected outcome**,
  encoded in `passed`, per `common/results.py`'s contract.
- **Fail-soft:** `run_checks` returns `ok(...)` (COMPLETED) for *every* check result,
  pass or fail. It returns `fail(...)` (FAILED) **only** for a genuine worker error —
  missing `repoPath`, `repoPath` not a directory, or `bash` failing to spawn.
  Consequence: a failing sub-task check never FAILs the forked sub-workflow, so it
  never poisons the `JOIN` — siblings still merge and `code_parallel` still completes.
- `stdout`/`stderr` are capped with `common.results.cap(..., OUTPUT_CAP)` so a chatty
  suite can't bloat the Conductor payload.

---

## 5. Gating behaviour

### 5.1 Sub-task gate (`code_subtask.json`)

```
worktree_add ──▶ coding_agent ──▶ run_checks(check) ──▶ SWITCH verify_gate
                                                          │  passed == true → commit
                                                          └─ default (false)  → (nothing)
```

- `check.input.cmd = ${workflow.input.testCmd}` (the planner's per-sub-task command).
- `SWITCH verify_gate` is `evaluatorType: "value-param"`, `expression: "passed"`,
  `inputParameters: { "passed": "${check.output.passed}" }`, mirroring the existing
  `design_gate` boolean switch in `code_parallel`. Case `"true"` contains `commit`;
  `defaultCase` is `[]`.
- **When the check passes (or nothing ran → `passed == true`)** → `commit` runs → the
  branch carries the work → `merge_worktrees` picks it up. No regression for sub-tasks
  the planner didn't give a `testCmd`.
- **When the check fails (`passed == false`)** → `commit` is skipped → the worktree
  branch `cc-group-<id>` has no new commit → `merge_worktrees` merges a no-op → **the
  broken edits never reach the change branch.** The sub-task output reports
  `verified: false`, satisfying *"flagged/failed rather than silently committed and
  merged."*
- The sub-workflow still **COMPLETES** (it is not TERMINATE-FAILED) so a single failed
  check does not fail the whole `FORK_JOIN_DYNAMIC` / `code_parallel`.

### 5.2 Integration gate (`code_parallel.json`)

```
… merge_worktrees(merge) ──▶ run_checks(verify) ──▶ aggregate
```

- `verify.input.cmd = ${workflow.input.verifyCmd}` (the caller's repo-wide test/build
  command). Empty → `_detect_default(repoPath)`.
- `verify.input.repoPath = ${workflow.input.repoPath}` — the integrated change branch.
- Its result is surfaced **verbatim** in `code_parallel`'s `outputParameters`
  (`verified`, `verifiedRan`, `verifyExitCode`, `verifyCmd`) and folded into the
  `aggregate` summary. This is the "real pass/fail verification result for the
  integrated branch."
- `verify` is **not** gated by a switch — the integration gate *reports*; it does not
  auto-fail the workflow (auto-merge / auto-fail is a follow-up scoring-loop concern).
  A caller reads `verified` (and `verifiedRan`) to decide.

### 5.3 Detection fallback (`_detect_default`)

Best-effort, ordered, first match wins; returns `None` if nothing matches (→
`ran = false`). Full table in [data-model.md](./data-model.md): `pyproject.toml` /
`pytest.ini` / `tox.ini` / `setup.cfg` → `pytest -q`; `package.json` with a `test`
script → `npm test --silent`; `Makefile` with a `test:` target → `make test`;
`go.mod` → `go test ./...`; `Cargo.toml` → `cargo test`.

---

## 6. Registration & rollout

- `workers/main.py`: `DEFAULT_MODULES = "coding_agent,gitops,checks"` — the new module
  is imported at startup so the `@worker_task` decorator registers `run_checks`.
- `workers/register.sh`: **no code change needed.** It globs `workflows/taskdefs/*.json`
  (picks up `run_checks.json` automatically) and its SIMPLE-task-coverage check passes
  because `run_checks.json` exists. The workflow-ordering list is unchanged (no new
  workflow is added).
- `code_subtask` / `code_parallel` keep `version: 1` — these are **in-place** edits to
  existing versions, consistent with how the repo pins sub-workflows at v1.

---

## 7. Acceptance-criteria traceability

| Acceptance criterion | Where satisfied |
|---|---|
| A sub-task whose edits fail its own `testCmd` is flagged/failed, not silently committed & merged. | §5.1 — `verify_gate` skips `commit`; `verified: false` in output. |
| `code_parallel` output includes a real pass/fail verification result for the integrated branch. | §5.2 — `verify` task + `verified` / `verifiedRan` / `verifyExitCode` outputs. |
| New unit tests for `run_checks` pass; existing `pytest tests/` stays green. | [testing.md](./testing.md) — `test_checks.py` + `test_workflows.py` edits. |
| No change to unrelated workflows (`design_docs`, `pr_review`, `address_pr`, `issue_to_pr`). | §3 layout touches only `code_subtask`, `code_parallel`, `main.py`, and the new/test files. |
