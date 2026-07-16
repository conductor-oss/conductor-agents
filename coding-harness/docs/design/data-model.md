# Data model — `run_checks`

> Supporting doc for [architecture.md](./architecture.md) (CCOR-13227). All names,
> types, and shapes here are **defined by architecture.md** and restated in
> implementation detail. If anything here diverges from architecture.md, that file
> wins.

This doc pins the exact Python types, constants, output field types, and the
`_detect_default` table for the `run_checks` worker in `workers/checks/tasks.py`.

---

## 1. Reused existing types (no changes)

`run_checks` builds on primitives that already exist — it introduces **no** new
dataclass or module-level type.

| Symbol | Source | Used for |
|---|---|---|
| `run(cmd, cwd=None, check=False) -> RunResult` | `common/exec.py` | Executes the check subprocess (stdin closed, output captured). Called with `check=False`. |
| `RunResult(stdout: str, stderr: str, code: int)` | `common/exec.py` | Return of `run()`. `code` is the process exit code. |
| `cap(s, limit=4000) -> str` | `common/results.py` | Truncates `stdout`/`stderr` for the payload/logs. |
| `ok(task, output, logs=None) -> TaskResult` | `common/results.py` | COMPLETED result (every pass/fail check outcome). |
| `fail(task, context, error, logs=None) -> TaskResult` | `common/results.py` | FAILED result (genuine worker error only). |

Module constant introduced by this change:

```python
OUTPUT_CAP = 4000   # chars kept from stdout/stderr, matching common.results.cap default
```

---

## 2. `run_checks` input

Read from `task.input_data or {}`.

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repoPath` | `str` | **yes** | — | Missing/empty or not a directory → `fail(...)`. |
| `cmd` | `str` | no | `""` | Empty/whitespace → `_detect_default(repoPath)`. |
| `label` | `str` | no | `""` | Log/summary tag only; never affects `passed`. |

Coercion: `cmd = (i.get("cmd") or "").strip()`. Conductor may pass a missing param as
`None` or `""`; both are treated as "not supplied".

---

## 3. `run_checks` output (`output_data`)

Exactly the shape in architecture.md §4. Types:

| Field | Type | Value |
|---|---|---|
| `cmd` | `str` | The command line actually used; `""` when nothing to run. |
| `ran` | `bool` | `True` iff a command executed. |
| `detected` | `bool` | `True` iff `cmd` came from `_detect_default` (input `cmd` was empty). |
| `passed` | `bool` | `code == 0` when `ran`; `True` when `not ran`. |
| `exitCode` | `int \| None` | `RunResult.code` when `ran`; `None` otherwise. |
| `stdout` | `str` | `cap(RunResult.stdout, OUTPUT_CAP)`; `""` when not run. |
| `stderr` | `str` | `cap(RunResult.stderr, OUTPUT_CAP)`; `""` when not run. |
| `durationMs` | `int` | Wall time of the subprocess (`0` when not run). |
| `summary` | `str` | One-line human summary (see §5). |

### `passed` truth table

| Condition | `ran` | `detected` | `exitCode` | `passed` |
|---|---|---|---|---|
| `cmd` supplied, exit 0 | `True` | `False` | `0` | `True` |
| `cmd` supplied, exit ≠ 0 | `True` | `False` | `≠0` | `False` |
| `cmd` empty, detected, exit 0 | `True` | `True` | `0` | `True` |
| `cmd` empty, detected, exit ≠ 0 | `True` | `True` | `≠0` | `False` |
| `cmd` empty, nothing detected | `False` | `False` | `None` | `True` (fail-open) |

---

## 4. `_detect_default(repo_path) -> str | None`

Best-effort, **ordered**, first match wins. All checks are file-existence /
simple-content probes rooted at `repo_path` — no subprocess, no network. Returns the
command string, or `None` when nothing matches.

| Order | Probe (relative to `repo_path`) | Returns |
|---|---|---|
| 1 | any of `pyproject.toml`, `pytest.ini`, `tox.ini`, `setup.cfg` exists | `"pytest -q"` |
| 2 | `package.json` exists **and** its JSON has a `.scripts.test` | `"npm test --silent"` |
| 3 | `Makefile` exists **and** contains a line matching `^test:` | `"make test"` |
| 4 | `go.mod` exists | `"go test ./..."` |
| 5 | `Cargo.toml` exists | `"cargo test"` |
| — | otherwise | `None` |

Notes:
- Probe 2 parses `package.json` defensively: a malformed file is treated as "no test
  script" (skip to the next probe), never a crash.
- This is deliberately minimal — richer multi-language detection is out of scope
  (architecture.md §1 non-goals). The planner's `testCmd` / caller's `verifyCmd` is the
  intended primary source; detection only spares callers from hand-specifying a command
  for the common Python case.

---

## 5. `summary` and log lines

`summary` format (`[label]` omitted when `label` is empty):

```
run_checks[<label>]: <pass|fail|skip> (exit <code|—>) cmd=`<cmd or (none)>`
```

- `pass` → `ran and passed`; `fail` → `ran and not passed`; `skip` → `not ran`.
- The worker also emits this as its single `ok(...)` log line, matching the terse
  `[task] …` log style used across `gitops/tasks.py`.

---

## 6. Workflow-visible fields (consumers)

The two gates read from the `run_checks` output above. The exact interpolations live in
[workflow-wiring.md](./workflow-wiring.md); the field-name mapping is:

**`code_subtask` (`check` ref → sub-workflow output):**

| Sub-workflow output key | Source |
|---|---|
| `verified` | `${check.output.passed}` |
| `checkRan` | `${check.output.ran}` |
| `checkExitCode` | `${check.output.exitCode}` |
| `checkCmd` | `${check.output.cmd}` |

**`code_parallel` (`verify` ref → workflow output):**

| Workflow output key | Source |
|---|---|
| `verified` | `${verify.output.passed}` |
| `verifiedRan` | `${verify.output.ran}` |
| `verifyExitCode` | `${verify.output.exitCode}` |
| `verifyCmd` | `${verify.output.cmd}` |

The per-sub-task `verified` flag is also projected into `aggregate`'s `perSubtask[]`
(see [workflow-wiring.md](./workflow-wiring.md) §3).
