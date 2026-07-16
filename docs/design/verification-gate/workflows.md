# Workflow & task-def wiring

> Reuses [architecture.md](./architecture.md) and [data-model.md](./data-model.md)
> verbatim. This doc gives the exact JSON edits for the two workflows, the new
> task def, and registration.

---

## 1. New task def — `workers/workflows/taskdefs/run_checks.json`

Modelled on `merge_worktrees.json` (long-running, alert-only) but with
`retryCount: 0` — re-running a deterministic failing suite is pointless.

```json
{
  "name": "run_checks",
  "description": "Run a project's test/build command as a pre-merge gate. Input: repoPath, cmd? (falls back to a repo-detected default). Output: passed/exitCode/stdout/stderr (capped). A failing check is COMPLETED with passed=false, not a worker error.",
  "retryCount": 0,
  "retryLogic": "FIXED",
  "retryDelaySeconds": 5,
  "timeoutSeconds": 0,
  "responseTimeoutSeconds": 86400,
  "timeoutPolicy": "ALERT_ONLY",
  "ownerEmail": "viren@orkes.io"
}
```

---

## 2. `code_subtask.json`

Three edits. Reference names follow architecture.md §4.6.

### 2.1 Add the `testCmd` input

Add `"testCmd"` to `inputParameters` and default it in `inputTemplate`:

```jsonc
"inputParameters": [ "repoPath", "name", "prompt", "promptTemplate",
  "templateKey", "promptContext", "model", "agent", "maxTurns",
  "maxBudgetUsd", "testCmd" ],
"inputTemplate": {
  "promptTemplate": "", "templateKey": "code", "promptContext": {},
  "maxTurns": 250, "maxBudgetUsd": 50.0,
  "testCmd": ""
}
```

### 2.2 Insert `checks` after `coding_agent`, and gate `commit`

The task list becomes `wt → code → checks → verify_gate(→ cmt)`. `commit`
(`cmt`) moves *inside* the switch's `"true"` branch; its `repoPath` still points
at the worktree (`${wt.output.worktreePath}`).

```jsonc
{
  "name": "run_checks",
  "taskReferenceName": "checks",
  "type": "SIMPLE",
  "inputParameters": {
    "repoPath": "${wt.output.worktreePath}",
    "cmd": "${workflow.input.testCmd}"
  }
},
{
  "name": "verify_gate",
  "taskReferenceName": "verify_gate",
  "type": "SWITCH",
  "evaluatorType": "value-param",
  "expression": "passed",
  "inputParameters": {
    "passed": "${checks.output.passed}"
  },
  "decisionCases": {
    "true": [
      {
        "name": "commit",
        "taskReferenceName": "cmt",
        "type": "SIMPLE",
        "inputParameters": {
          "repoPath": "${wt.output.worktreePath}",
          "message": "code_subtask ${workflow.input.name}"
        }
      }
    ]
  },
  "defaultCase": []
}
```

- `passed == true` (check passed **or** skipped) → `commit` runs, exactly as
  today. Backward-compatible: a plan with no `testCmd` ⇒ `checks` skips ⇒
  `passed=true` ⇒ still commits.
- `passed == false` → `defaultCase` (empty) → **no commit**. The worktree branch
  gains no new commit, so `merge_worktrees` merges nothing broken from it.

The switch mirrors the existing `design_gate` in `code_parallel.json` (also
`value-param`, boolean stringified to `"true"`), so no new pattern is
introduced. `defaultCase: []` is the sanctioned "do nothing" escape hatch (see
`test_no_dangling_switch_branches`).

### 2.3 Output parameters

```jsonc
"outputParameters": {
  "status": "${code.output.status}",
  "agent": "${code.output.agent}",
  "model": "${code.output.model}",
  "filesChanged": "${code.output.filesChanged}",
  "costUsd": "${code.output.costUsd}",
  "tokenUsed": "${code.output.tokenUsed}",
  "numTurns": "${code.output.numTurns}",
  "turns": "${code.output.turns}",
  "branch": "${wt.output.branch}",
  "commit": "${cmt.output.commit}",
  "verified": "${checks.output.passed}",
  "checkSkipped": "${checks.output.skipped}",
  "checkExitCode": "${checks.output.exitCode}"
}
```

`commit` resolves to `null` when `cmt` did not run (failed check) — this is how a
caller distinguishes a merged sub-task from a flagged one.

---

## 3. `code_parallel.json`

Three edits.

### 3.1 Add the `verifyCmd` input

Add `"verifyCmd"` to `inputParameters` and `inputTemplate`:

```jsonc
"inputTemplate": {
  …existing…,
  "verifyCmd": ""
}
```

Empty ⇒ `run_checks` runs `detect_default_cmd(repoPath)` (or skips if nothing is
detected). The planner-supplied per-sub-task `testCmd`s cover the sub-tasks; the
repo-level `verifyCmd` covers the integrated branch.

### 3.2 `build_forks` passes `testCmd` into each sub-task

Today the `build_forks` JQ folds `testCmd` only into the prompt text. Add it as a
**structured** field on each `dynamicTasksInput` entry so `code_subtask` can run
it. The `dynamicTasksInput` object per sub-task gains `testCmd:($s.testCmd //
"")`; the existing `"Your check: …"` prompt text is kept (it still informs the
agent). The relevant fragment of the `queryExpression`:

```
{($s.id): {
   repoPath:$r, name:$s.id, model:$m, agent:$ag,
   maxTurns:$mt, maxBudgetUsd:$mb,
   testCmd:($s.testCmd // ""),
   prompt:($desc + "…"), promptTemplate:$ct,
   templateKey:"code", promptContext:{subtask:$desc}
}}
```

No other part of `build_forks` changes.

### 3.3 Insert `verify` after `merge`, before `aggregate`

```jsonc
{
  "name": "run_checks",
  "taskReferenceName": "verify",
  "type": "SIMPLE",
  "inputParameters": {
    "repoPath": "${workflow.input.repoPath}",
    "cmd": "${workflow.input.verifyCmd}"
  }
}
```

Placed between the `merge` (`merge_worktrees`) task and the `aggregate`
`JSON_JQ_TRANSFORM`. It runs against the integrated change branch in the main
repo path (not a worktree).

### 3.4 `aggregate` carries `verified` per sub-task

In the `aggregate` `queryExpression`, extend the per-sub-task projection to
include `verified`:

```
{ status:(.status // "unknown"),
  filesChanged:(.filesChanged // []),
  costUsd:(.costUsd | num),
  tokenUsed:(.tokenUsed | num),
  verified:(.verified // null) }
```

`aggregate` stays `optional: true`; no other reductions change.

### 3.5 Output parameters

Add to the existing `outputParameters`:

```jsonc
"verified": "${verify.output.passed}",
"verification": "${verify.output}"
```

`merged`, `conflicts`, `subtasks`, `mergeCostUsd`, `totalTokens`,
`totalCostUsd`, `summary`, `changeBranch`, `groupIds` are unchanged.

---

## 4. `main.py`

```python
DEFAULT_MODULES = "coding_agent,gitops,test"
```

so the `run_checks` poller loads by default. Update the module docstring's
parenthetical to mention `test` carries `run_checks`. Any host overriding
`WORKER_MODULES` must include `test`.

---

## 5. Registration

`workers/register.sh` globs `workflows/*.json` and `workflows/taskdefs/*.json`,
so `taskdefs/run_checks.json` registers automatically once present — no manual
list to edit. The edited `code_subtask.json` / `code_parallel.json` re-register
in place (same `name`; additive input params need no version bump).

---

## 6. Invariant compatibility (test_workflows.py)

These edits keep every existing invariant green:

- **SIMPLE-task coverage** — `run_checks` SIMPLE nodes are backed by
  `taskdefs/run_checks.json`.
- **`@worker_task` parity** — `run_checks` is decorated in `test/tasks.py`; the
  invariant test's module scan must add `"test/tasks.py"` (see
  [testing.md](./testing.md)).
- **Reference resolution** — `${checks.output.*}`, `${verify.output.*}`,
  `${wt.output.*}`, `${cmt.output.*}` all resolve to refs present in their
  workflow (including inside the `verify_gate` switch, which `_collect_tasks`
  descends into).
- **No dangling switch** — `verify_gate`'s `"true"` case holds a well-formed
  `commit`; the empty `defaultCase` is allowed.
- **Reference-name uniqueness** — `checks`, `verify`, `verify_gate` are new and
  unique within their workflows.
- **Untouched workflows** — `design_docs`, `pr_review`, `address_pr`,
  `issue_to_pr`, `github_demo` are not edited.
