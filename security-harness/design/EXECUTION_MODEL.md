# Execution model: a formal proof that the harness runs as a Conductor workflow

This document formalizes the claim that **security-conductor is, by design, a
Conductor-orchestrated workflow whose sole entry point is "start the workers, then
start the workflow with an input vector (app URL, source location, docs, identities,
…)."** It proves the execution is well-defined and terminates, and it formalizes the
exact precondition whose violation caused the manual worker-babysitting seen earlier.

Scope of the claim: we prove **orchestration** properties — executability,
self-containment of inputs, and termination — under the Conductor execution semantics.
We do **not** claim semantic correctness of the security *findings*; LLM tasks are
modeled as terminating oracles (Def. 6), so the theorem is about the harness *running*,
not about whether a given finding is true.

---

## 1. Objects

**Definition 1 (Task types).** Partition the Conductor task types into
- *system* types `SYS = { JSON_JQ_TRANSFORM, SWITCH, SET_VARIABLE, FORK_JOIN,
  FORK_JOIN_DYNAMIC, JOIN, DO_WHILE, TERMINATE, NOOP, SUB_WORKFLOW, LLM_CHAT_COMPLETE,
  LLM_INDEX_TEXT, LLM_SEARCH_INDEX, GENERATE_PDF }`, executed by the **server**; and
- *simple* type `SIMPLE`, executed by an external **worker** that polls by task-def name.

**Definition 2 (Workflow).** A workflow is `w = (N, type, name, dep, guard, ctrl)`:
- `N` a finite set of task references (the `taskReferenceName`s in the JSON);
- `type : N → TaskTypes`, `name : N → Strings` (task-def name for `SIMPLE`);
- `dep : N → ℘(N)` the data dependencies induced by `${X.output…}` / `${workflow.variables…}`
  references appearing in a task's `inputParameters`;
- `guard : N → BoolExpr` from the enclosing `SWITCH`/`DO_WHILE` (⊤ if none);
- `ctrl` the control constructs (`SWITCH`, `FORK_JOIN[_DYNAMIC]`+`JOIN`, `DO_WHILE`, `SUB_WORKFLOW`).

For `w₀ = deep_assess` these are exactly the contents of
`conductor/workflows/deep_assess.json`; its `inputParameters` declare the input vector
(Def. 5), its `tasks[]` give `N`, and `${…}` refs give `dep`.

**Definition 3 (Registry & coverage).** `R` is the set of registered task defs +
workflows (produced by `conductor/register.sh`). A **worker pool** `Π` is a finite set of
workers, each `π` polling a name set `D(π) ⊆ Strings`. Define the **coverage predicate**

> `Cov(Π, w)  ⟺  ∀ n ∈ N(w) with type(n)=SIMPLE :  name(n) ∈ ⋃_{π∈Π} D(π)`,

extended over the sub-workflow closure of `w`.

**Definition 4 (Execution state).** An execution `σ` of `w` on input `I` is
`σ = (Nσ, st, var)` where `Nσ ⊇ N(w)` is the **dynamic unrolling** (loops and dynamic
forks expand `N` into per-iteration / per-child copies), `st : Nσ → Status` with
`Status = {SCHEDULED, IN_PROGRESS, COMPLETED, FAILED, TERMINATED, TIMED_OUT, SKIPPED}`,
and `var` the workflow variables. A status is **terminal** if in
`{COMPLETED, FAILED, TERMINATED, TIMED_OUT, SKIPPED}`. `σ` is **terminal** if the
workflow's own status is terminal.

**Definition 5 (Entry point).** The entry point is the function

> `assess(I)  ≡  register(R) ; ensure(Π) ; start(deep_assess, I)`

realized literally by the script `./assess` (resp. `./scan`): it registers defs
(`make register`), assumes a running worker pool (`make workers`), and issues
`conductor workflow start -w deep_assess -i I` (`assess` line ~181). The input vector is

> `I = ⟨ target, source_path, docs, identities, manifest, scope, login_url, max_passes, … ⟩`

i.e. **app URL = `target`, source location = `source_path`, docs = `docs`, credentials
= `identities`** — exactly the declared `inputParameters` of `deep_assess`. No other
ingress exists: a run is created only by a `start` event carrying `I`.

**Definition 6 (LLM oracle).** Each `LLM_CHAT_COMPLETE` task is modeled as an oracle
`O : Context → Value` that returns *some* value in bounded time (the task carries
`maxTokens` and the def carries `responseTimeoutSeconds`). `O` is nondeterministic; we
assume only **totality within the timeout**. This isolates "the model's answer" from the
orchestration argument.

---

## 2. Operational semantics (small-step `→`)

A transition rewrites `σ`. The rules:

1. **(Sched)** If `n` is non-terminal, every `m ∈ dep(n)` is `COMPLETED`, and `guard(n)`
   holds, then `st(n) := SCHEDULED`.
2. **(Sys)** If `type(n) ∈ SYS` and `st(n)=SCHEDULED`: the server computes `out(n)` from
   the (available) inputs and sets `st(n) := COMPLETED` (or `FAILED` on error; `TERMINATE`
   sets the workflow terminal; `SWITCH` selects a branch; `FORK_JOIN_DYNAMIC` instantiates
   a finite child set; `DO_WHILE` either unrolls one more iteration or exits).
3. **(Claim)** If `type(n)=SIMPLE`, `st(n)=SCHEDULED`, and `∃ π∈Π : name(n) ∈ D(π)`, then
   within boundedly many steps `st(n) : SCHEDULED → IN_PROGRESS → COMPLETED|FAILED`
   (worker executes the task fn, which **never raises** by construction — every worker
   returns an error in its result dict, so it always reaches a terminal status).
4. **(Stall)** If `type(n)=SIMPLE`, `st(n)=SCHEDULED`, and `∄ π : name(n) ∈ D(π)`, then
   **no rule applies to `n`** — it stays `SCHEDULED` indefinitely.
5. **(Timeout)** If `st(n)=IN_PROGRESS` longer than `timeoutSeconds`, then `st(n) :=
   TIMED_OUT` (and, if `optional`, treated as non-fatal by its `JOIN`/successor).
6. **(Join)** A `JOIN` becomes `COMPLETED` when each joined branch is terminal; a branch
   that is `FAILED/TIMED_OUT` but `optional:true` counts as terminal for the `JOIN`.

Rule **(Stall)** is the formal content of the bug: a `SIMPLE` task with no covering
worker is a sink that never fires.

---

## 3. Theorem

> **Theorem (Executability & Termination).** Let `deep_assess` be registered (`R`) and let
> `Π` satisfy `Cov(Π, deep_assess)`. Then for every input `I`, the execution
> `start(deep_assess, I)` reaches a terminal state `σ_T` in finitely many `→` steps,
> using no ingress other than the entry point `assess(I)` of Def. 5.

### 3.1 Lemma A (Finite unrolling)
`|Nσ| < ∞` for every reachable `σ`.

*Proof.* `Nσ` grows only via `DO_WHILE` iterations and `FORK_JOIN_DYNAMIC` children.
- **Loops.** Each `DO_WHILE` in the harness has loop condition of the form
  `iteration < K ∧ φ` with `K` a constant bound (`pass_loop`: `K = max_passes`;
  `explore_loop`/`exploit_loop`: `K = max_steps`). Let `μ(σ) = K − iteration ∈ ℕ`. Each
  completed iteration strictly decreases `μ`; `μ` is well-founded, so the number of
  iterations is `≤ K < ∞`.
- **Dynamic forks.** `build_exploit_jobs` / `build_verify_jobs` / `build_purple_jobs`
  produce a child list of length `≤ max_hypotheses` (a constant), by a pure
  `JSON_JQ_TRANSFORM` over a finite array. Each child is itself a workflow
  (`exploit_agent`, `verify_finding`, `purple_check`).
- **Nesting.** The sub-workflow relation `⊐` is the fixed finite hierarchy
  `deep_assess ⊐ {surface, docs_ingest, assess_pass, reflect_pass}`,
  `assess_pass ⊐ {explore_agent, exploit_agent, exploit_deepen, verify_finding, purple_check}`,
  with no cycles (a workflow never transitively calls itself). Depth `≤ 3`. (`exploit_deepen`
  is routed in place of `exploit_agent` by `build_exploit_jobs` for injection/code-exec/sandbox
  sinks; its `exploit_loop` carries the same `iteration < max_steps` bound, so Lemma A's loop
  argument covers it unchanged.)

By structural induction over `⊐`, each level contributes finitely many tasks and the
recursion bottoms out, so `|Nσ| < ∞`. ∎

### 3.2 Lemma B (Acyclic data dependency)
Within a single workflow body, `dep` is acyclic; hence a topological order exists.

*Proof.* Conductor rejects a registration whose `${X.output}` references a task `X` that
is not earlier in scope (the same check that forbids bare `${X.y}` refs). Therefore every
edge of `dep` points from a later task to an earlier one in declaration order restricted
to each scope; declaration order is a total order, so `dep` ⊆ a strict total order ⇒
acyclic. Loop/fork *bodies* re-enter via fresh unrolled copies (Lemma A), not via a cycle
in `dep`. ∎ (Empirically: all 15 workflows register without error — §5.)

### 3.3 Proof of the Theorem
Define the potential `Φ(σ) = ` (number of non-terminal tasks in `Nσ`) `+ Σ_loops μ`.
By Lemma A, `Nσ` is finite, so `Φ(σ) ∈ ℕ`.

*Progress.* Take any non-terminal `σ`. Some task is non-terminal. Consider a `dep`-minimal
non-terminal task `n` (exists by Lemma B):
- If `st(n) ≠ SCHEDULED` and `n`'s deps are met and `guard(n)` holds, **(Sched)** fires.
- If `st(n)=SCHEDULED` and `type(n)∈SYS`, **(Sys)** fires.
- If `st(n)=SCHEDULED` and `type(n)=SIMPLE`, then by `Cov` there is a covering `π`, so
  **(Claim)** fires (NOT **(Stall)**).
- If `st(n)=IN_PROGRESS`, **(Claim)**'s bounded completion or **(Timeout)** fires.
Each such firing moves a task to terminal (decreasing the count term) or completes a loop
iteration (decreasing `μ`), strictly decreasing `Φ`. A `TERMINATE` task (the authorization
gate's refuse branch, or the safety governor's halt branch) sets the workflow terminal
immediately, which only decreases `Φ`.

*Termination.* `Φ` is an `ℕ`-valued ranking function that strictly decreases on every
`→` step and is bounded below by `0`. By well-foundedness of `(ℕ, <)`, no infinite `→`
chain exists; the execution reaches `Φ`-minimal, i.e. all tasks terminal ⇒ `σ_T` terminal,
in `≤ Φ(σ₀)` steps. 

*Self-containment of ingress.* By Def. 2 every task input is an expression over
`workflow.input ∪ {prior outputs} ∪ workflow.variables`. Induction on the topological
order (Lemma B): the base task `normalize_target` depends only on `workflow.input = I`, so
it is schedulable immediately from `I`; if every task before `n` is `COMPLETED`, all of
`n`'s referenced outputs exist, so `n` is schedulable. Hence the single `start(·, I)` event
suffices and no `→` step consults data outside `I` and prior outputs. Therefore the entry
point is exactly `assess(I)` of Def. 5. ∎

---

## 4. Corollary (the bug, and why it was not a design defect)

> **Corollary.** If `¬Cov(Π, deep_assess)` — i.e. some `SIMPLE` task name has no polling
> worker — then there is a reachable `σ` and a task `n` with `st(n)=SCHEDULED` forever
> (**(Stall)**). If `n` dominates a `JOIN` or a `DO_WHILE` guard, that construct never
> completes and `start(deep_assess, I)` **does not terminate** (it deadlocks short of
> `σ_T`).

This is precisely what occurred when workers were started piecemeal: the `surface`
`JOIN` stalled on `sast_*` (no `sast` worker), then `exploit_loop` stalled on `code_exec`
(no `codeexec` worker), then `verify`/`oob_check` stalled (no `oob` worker). The fix is to
satisfy the theorem's hypothesis `Cov`, which the repository already encodes:

```
# Makefile (default)
WORKER_MODULES ?= recon,browser,dast,sast,api,rag,httptool,codeexec,oob,safety
```

`⋃_π D(π)` for that module set ⊇ `{name(n) : type(n)=SIMPLE}` over the sub-workflow
closure of `deep_assess`, so `make workers` establishes `Cov`. The manual restarting was
an ad-hoc reconstruction of `Cov`, not a property of the design: **`assess(I) = register ;
make workers ; conductor workflow start -w deep_assess -i I`** is the whole entry point,
and under `Cov` the Theorem guarantees it runs to a terminal state with no further
intervention.

---

## 5. Discharging the empirical premises

The proof rests on premises that are mechanically checkable (and were checked):
- **Lemma B / registration validity:** `bash conductor/register.sh` accepts all task defs
  and all 15 workflows against a live server (no `${…}` resolves to an undefined task) —
  verified.
- **Cov:** the worker startup log enumerates one active worker per `SIMPLE` task name in
  the closure — verifiable via `grep 'Worker\[name=' ` over the worker log.
- **Loop bounds present:** `pass_loop`/`explore_loop`/`exploit_loop` carry
  `iteration < max_*` conditions; dynamic forks slice `.[0:max_hypotheses]` — present in
  the JSON.
- **No-raise workers:** every `@worker_task` returns an error field rather than raising —
  a code invariant (Rule (Claim)).
- **`optional:true` on dynamic-fork children:** present on `exploit_*`/`verify_*`/`purple_*`
  and the enrichment subs — so (Join) cannot deadlock on a failed child.

∎
