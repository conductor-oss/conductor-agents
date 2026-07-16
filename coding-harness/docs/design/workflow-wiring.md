# Workflow wiring — exact JSON edits

> Supporting doc for [architecture.md](./architecture.md) (CCOR-13227). Every task
> name (`run_checks`), reference name (`check`, `verify_gate`, `verify`), and input
> name (`testCmd`, `verifyCmd`) is **defined in architecture.md** and used verbatim
> here. If anything diverges, architecture.md wins.

This doc gives the concrete JSON for the new taskdef and the surgical edits to
`code_subtask.json` and `code_parallel.json`. **No other workflow is touched.**

---

## 1. New taskdef — `workers/workflows/taskdefs/run_checks.json`

Modeled on `taskdefs/commit.json`. `retryCount: 1` covers a transient worker crash;
it never retries a *failing check* because `run_checks` returns COMPLETED (not FAILED)
for a failing command (architecture.md §4).

```json
{
  "name": "run_checks",
  "description": "Run a repo's test/build command in a working dir and report pass/fail (exit code + capped stdout/stderr). Fail-soft: a failing check is COMPLETED with passed=false, not FAILED. Input: repoPath, cmd?, label?.",
  "retryCount": 1,
  "retryLogic": "FIXED",
  "retryDelaySeconds": 5,
  "timeoutSeconds": 0,
  "responseTimeoutSeconds": 86400,
  "timeoutPolicy": "ALERT_ONLY",
  "ownerEmail": "viren@orkes.io"
}
```

`register.sh` picks this up automatically via its `workflows/taskdefs/*.json` glob;
no edit to `register.sh` is required (architecture.md §6).

---

## 2. `code_subtask.json` — sub-task gate

### 2.1 Add the `testCmd` input

Add `"testCmd"` to `inputParameters` and a default to `inputTemplate`:

```jsonc
"inputParameters": [
  "repoPath", "name", "prompt", "promptTemplate", "templateKey",
  "promptContext", "model", "agent", "maxTurns", "maxBudgetUsd",
  "testCmd"
],
"inputTemplate": {
  "promptTemplate": "",
  "templateKey": "code",
  "promptContext": {},
  "maxTurns": 250,
  "maxBudgetUsd": 50.0,
  "testCmd": ""
}
```

### 2.2 Insert `run_checks` and gate `commit`

The `tasks` array becomes: `worktree_add` (`wt`) → `coding_agent` (`code`) →
**`run_checks` (`check`)** → **`SWITCH verify_gate`** whose `"true"` case holds the
**unchanged** `commit` (`cmt`). The old top-level `commit` task is **moved inside** the
switch's `"true"` case (it keeps `repoPath: "${wt.output.worktreePath}"` and its
message).

```json
{
  "name": "run_checks",
  "taskReferenceName": "check",
  "type": "SIMPLE",
  "inputParameters": {
    "repoPath": "${wt.output.worktreePath}",
    "cmd": "${workflow.input.testCmd}",
    "label": "${workflow.input.name}"
  }
},
{
  "name": "verify_gate",
  "taskReferenceName": "verify_gate",
  "type": "SWITCH",
  "evaluatorType": "value-param",
  "expression": "passed",
  "inputParameters": {
    "passed": "${check.output.passed}"
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

Boolean `value-param` switching on `"true"` matches the existing `design_gate` in
`code_parallel.json`. When `passed` is `false`, no case matches → `defaultCase` (empty)
→ `commit` never runs → the branch has no new commit → `merge_worktrees` merges a
no-op (architecture.md §5.1).

### 2.3 Output parameters

Add the verification fields; `commit` still resolves (`null`/unresolved when the gate
skipped it — the same pattern `code_parallel`'s `aggregate` already relies on for the
optional `design` task):

```json
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
  "verified": "${check.output.passed}",
  "checkRan": "${check.output.ran}",
  "checkExitCode": "${check.output.exitCode}",
  "checkCmd": "${check.output.cmd}"
}
```

---

## 3. `code_parallel.json` — integration gate

### 3.1 Add the `verifyCmd` input

```jsonc
"inputParameters": [ …existing…, "verifyCmd" ],
"inputTemplate": { …existing…, "verifyCmd": "" }
```

### 3.2 Thread `testCmd` into the dynamic sub-task inputs (`build_forks`)

`build_forks` already interpolates `testCmd` into the prompt as `"Your check: …"`
text. **Keep that** and *additionally* pass it as a structured `testCmd` input so
`code_subtask`'s `check` task can execute it. In the `reduce` that builds each
sub-task's input object, add one key — `testCmd:($s.testCmd // "")`:

```jsonc
// … {($s.id): {repoPath:$r, name:$s.id, model:$m, agent:$ag, maxTurns:$mt,
//   maxBudgetUsd:$mb, prompt:($desc + "…"), promptTemplate:$ct,
//   templateKey:"code", promptContext:{subtask:$desc},
   testCmd:($s.testCmd // "")
// }}
```

No other part of the `queryExpression` changes; `groupIds` and `dynamicTasks` are
unaffected.

### 3.3 Insert `run_checks` (`verify`) after `merge_worktrees`, before `aggregate`

Between the `merge` task and the `aggregate` task:

```json
{
  "name": "run_checks",
  "taskReferenceName": "verify",
  "type": "SIMPLE",
  "inputParameters": {
    "repoPath": "${workflow.input.repoPath}",
    "cmd": "${workflow.input.verifyCmd}",
    "label": "integration"
  }
}
```

This runs the repo-wide command against the merged change branch. It is **not** gated
by a switch — it reports (architecture.md §5.2).

### 3.4 Fold verification into `aggregate` + workflow output

`aggregate` gains two inputs and its `queryExpression` gains: a per-sub-task `verified`
projection and two top-level result fields. The updated `aggregate` task
(`optional: true` unchanged):

```json
{
  "name": "aggregate",
  "taskReferenceName": "aggregate",
  "type": "JSON_JQ_TRANSFORM",
  "optional": true,
  "inputParameters": {
    "joined": "${fan_join.output}",
    "planTokens": "${plan.output.tokenUsed}",
    "planCost": "${plan.output.costUsd}",
    "designTokens": "${design.output.tokenUsed}",
    "designCost": "${design.output.costUsd}",
    "mergeTokens": "${merge.output.tokenUsed}",
    "mergeCost": "${merge.output.costUsd}",
    "verifyPassed": "${verify.output.passed}",
    "verifyRan": "${verify.output.ran}",
    "queryExpression": "def num: if type == \"number\" then . else 0 end; (.joined // {}) as $j | [$j | to_entries[] | (.value.output // .value // {}) | {status:(.status // \"unknown\"), filesChanged:(.filesChanged // []), verified:(.verified // null), costUsd:(.costUsd | num), tokenUsed:(.tokenUsed | num)}] as $g | (.planTokens | num) as $pt | (.planCost | num) as $pc | (.designTokens | num) as $dt | (.designCost | num) as $dc | (.mergeTokens | num) as $mt | (.mergeCost | num) as $mc | ([$g[].tokenUsed] | add | num) as $st | ([$g[].costUsd] | add | num) as $sc | {perSubtask:$g, subtaskCount:($g | length), verified:.verifyPassed, verifiedRan:.verifyRan, tokens:{plan:$pt, design:$dt, subtasks:$st, merge:$mt}, cost:{plan:$pc, design:$dc, subtasks:$sc, merge:$mc}, totalTokens:($pt + $dt + $st + $mt), totalCostUsd:($pc + $dc + $sc + $mc)}"
  }
}
```

The only `queryExpression` changes vs. today are: `verified:(.verified // null)` in the
`perSubtask` projection, and `verified:.verifyPassed, verifiedRan:.verifyRan` in the
result object. Everything else (the `num` guard, token/cost math) is byte-for-byte
unchanged.

### 3.5 Workflow `outputParameters`

Add the four integration-gate fields alongside the existing ones:

```json
"outputParameters": {
  "changeBranch": "${workflow.input.changeBranch}",
  "groupIds": "${build_forks.output.result.groupIds}",
  "subtasks": "${plan.output.structured.subtasks}",
  "merged": "${merge.output.merged}",
  "conflicts": "${merge.output.conflicts}",
  "mergeCostUsd": "${merge.output.costUsd}",
  "verified": "${verify.output.passed}",
  "verifiedRan": "${verify.output.ran}",
  "verifyExitCode": "${verify.output.exitCode}",
  "verifyCmd": "${verify.output.cmd}",
  "totalTokens": "${aggregate.output.result.totalTokens}",
  "totalCostUsd": "${aggregate.output.result.totalCostUsd}",
  "summary": "${aggregate.output.result}"
}
```

---

## 4. `workers/main.py`

```python
DEFAULT_MODULES = "coding_agent,gitops,checks"
```

(One-line change; keeps the "which modules load is controlled by `WORKER_MODULES`"
comment accurate by also mentioning `checks` in the module docstring.)

---

## 5. Invariants preserved (why the wiring tests stay green)

- Every new `SIMPLE` task is named `run_checks` and has a matching
  `taskdefs/run_checks.json` → `test_every_simple_task_has_a_taskdef` passes.
- `run_checks` maps to a real `@worker_task` once `checks/tasks.py` is added to the
  scan set in `test_workflows.py` (see [testing.md](./testing.md)).
- Every new `${…}` interpolation (`${check.…}`, `${verify.…}`, `${verify_gate}` has no
  output refs) points at a task reference that exists in the same workflow →
  `test_all_output_references_resolve` passes.
- `verify_gate`'s `"true"` decision case is a non-empty, well-formed task list; its
  `defaultCase` is empty (allowed) → `test_no_dangling_switch_branches` passes.
- `taskReferenceName`s (`check`, `verify_gate`, `cmt`, `verify`) are unique within
  each workflow → `test_task_reference_names_unique_within_workflow` passes.
- `design_docs`, `pr_review`, `address_pr`, `issue_to_pr`, `github_demo` JSON is
  untouched.
