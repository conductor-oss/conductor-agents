# Data model — `run_checks`

> Reuses [architecture.md](./architecture.md) verbatim. Every name here is the
> canonical name defined there (§4).

This doc specifies the exact I/O of `common.checks` and the `run_checks` worker,
plus the resolution logic. It is the reference the implementation and tests
(`workers/tests/test_checks.py`) assert against field-by-field.

---

## 1. `common/checks.py`

### 1.1 Module constants

```python
STDIO_CAP = 4000   # chars; the limit passed to common.results.cap
```

### 1.2 `detect_default_cmd(repo_path: str) -> str | None`

Best-effort, deliberately small (broader multi-language detection is **out of
scope**, per the ticket). Checks are evaluated in this fixed priority order; the
first match wins:

| Priority | Signal (relative to `repo_path`) | Returned command |
|---|---|---|
| 1 | `pyproject.toml` **or** `pytest.ini` **or** a `tests/` directory | `"pytest -q"` |
| 2 | `package.json` containing a `"test"` script under `"scripts"` | `"npm test"` |
| 3 | `Makefile` containing a `test:` target | `"make test"` |
| 4 | `go.mod` | `"go test ./..."` |
| 5 | `Cargo.toml` | `"cargo test"` |
| — | none of the above | `None` |

- File presence is checked with `os.path` / `pathlib`; no subprocess is spawned.
- `package.json` is parsed with `json`; a parse error is treated as "no match"
  (fall through), never an exception out of `detect_default_cmd`.
- The function is pure and side-effect-free — safe to unit-test directly.

### 1.3 `run_checks(repo_path: str, cmd: str | None = None) -> dict`

Resolution order for the command:

1. `cmd` argument, if a non-empty string → run it (`detected=false`).
2. else `detect_default_cmd(repo_path)` → if not `None`, run it (`detected=true`).
3. else **skip**: return the skipped shape (nothing to verify).

When a command runs:

```python
res = exec.run(["bash", "-c", resolved], cwd=repo_path, check=False)  # RunResult
```

`check=False` is mandatory — a non-zero exit must return a `RunResult`, never
raise `RunError`. `stdout`/`stderr` are capped with `results.cap(res.stdout,
STDIO_CAP)`.

---

## 2. Output dict (canonical — architecture.md §4.3)

Returned by `common.checks.run_checks` and set verbatim as the `run_checks`
worker's `output_data`.

| Field | Type | Notes |
|---|---|---|
| `passed` | `bool` | `exitCode == 0` when a command ran; `true` when skipped. |
| `skipped` | `bool` | `true` iff no command ran (no `cmd`, no detected default). |
| `detected` | `bool` | `true` iff the command came from `detect_default_cmd`. |
| `cmd` | `str` | The resolved command actually run; `""` when skipped. |
| `exitCode` | `int \| null` | Process exit code; `null` when skipped. |
| `stdout` | `str` | Captured stdout, capped to `STDIO_CAP`. |
| `stderr` | `str` | Captured stderr, capped to `STDIO_CAP`. |

### 2.1 Canonical shapes

**Passed (explicit cmd):**
```json
{ "passed": true, "skipped": false, "detected": false,
  "cmd": "pytest -q", "exitCode": 0, "stdout": "…", "stderr": "" }
```

**Failed:**
```json
{ "passed": false, "skipped": false, "detected": false,
  "cmd": "pytest -q", "exitCode": 1,
  "stdout": "…", "stderr": "E   assert 1 == 2" }
```

**Detected default (no cmd supplied, repo looks like pytest):**
```json
{ "passed": true, "skipped": false, "detected": true,
  "cmd": "pytest -q", "exitCode": 0, "stdout": "…", "stderr": "" }
```

**Skipped (no cmd, nothing detected):**
```json
{ "passed": true, "skipped": true, "detected": false,
  "cmd": "", "exitCode": null, "stdout": "", "stderr": "" }
```

### 2.2 Invariants (asserted in tests)

- `skipped == true` ⇒ `passed == true` **and** `cmd == ""` **and**
  `exitCode == null` **and** `detected == false`.
- `skipped == false` ⇒ `passed == (exitCode == 0)` **and** `cmd != ""`.
- `detected == true` ⇒ `skipped == false` (a command ran).
- `stdout` / `stderr` never exceed `STDIO_CAP` (plus `cap`'s truncation notice).

---

## 3. Worker `TaskResult` mapping

Built with `common/results.py`:

- **Success (check ran or skipped):** `ok(task, out, ["[run_checks] …"])`.
  `out` is the §2 dict. A red suite is still `COMPLETED` — `passed=false` lives
  in `output_data`. This is intentional (architecture.md §1).
- **Genuine error** (e.g. `repoPath` missing, `bash` not found): `fail(task,
  "run_checks", e)`. `fail()` captures the full error + any `stdout`/`stderr`
  attribute in the Conductor logs tab.

The success log line is:
`[run_checks] {pass|FAIL|skipped} cmd={cmd!r} exit={exitCode}`.

---

## 4. Workflow-level shapes

### 4.1 `code_subtask` — how `passed` is consumed

The `verify_gate` `SWITCH` (`evaluatorType: "value-param"`, `expression:
"passed"`) reads the boolean from `${checks.output.passed}`. Booleans stringify
to `"true"` / `"false"` (same as the existing `design_gate` switch on
`design`), so the decision case key is `"true"`.

`code_subtask` `outputParameters` gain (existing fields unchanged):

| Output key | Source | Meaning |
|---|---|---|
| `verified` | `${checks.output.passed}` | Whether the sub-task's own check passed (or was skipped). |
| `checkSkipped` | `${checks.output.skipped}` | No check ran for this sub-task. |
| `checkExitCode` | `${checks.output.exitCode}` | Exit code of the sub-task's check. |
| `commit` | `${cmt.output.commit}` | `null` when `verify_gate` routed away from `commit` (i.e. the check failed). |

A failed sub-task therefore has `verified == false` and `commit == null`.

### 4.2 `code_parallel` — integrated-branch verification

`code_parallel` `outputParameters` gain:

| Output key | Source | Meaning |
|---|---|---|
| `verified` | `${verify.output.passed}` | Real pass/fail of the merged change branch. |
| `verification` | `${verify.output}` | Full §2 dict for the integrated run. |

The `aggregate` JQ additionally carries `verified` per sub-task, reading
`.verified` from each joined `code_subtask` output:

```
perSubtask[]: { status, filesChanged, costUsd, tokenUsed, verified }
```

`verified` is `null` for any sub-task output missing the field (defensive
`.verified // null`).

---

## 5. Field-name compatibility

All new keys are `camelCase` and do not collide with existing `code_subtask` /
`code_parallel` outputs (`status`, `filesChanged`, `costUsd`, `tokenUsed`,
`numTurns`, `turns`, `branch`, `commit`, `merged`, `conflicts`, `mergeCostUsd`,
`totalTokens`, `totalCostUsd`, `summary`). `run_checks` produces **no** cost or
token fields — it runs no LLM.
