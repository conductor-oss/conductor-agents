# Tasks

Phase 0 (groups 1–5) is independently shippable and leaves both harnesses more capable. Phase 1 (groups 6–10) is the GO/NO-GO spike and depends on groups 1–3. Honor `AGENTS.md` throughout: one workflow version (v1), zero new JQ tasks, no execution timeouts, simplicity-justified additions, honest `test_cycle` outcomes, precise-failure-mechanism terminology.

## 1. Decide the shared-library home and extract the generic substrate

Files: security-harness/workers/common/blackboard.py, (new) shared-substrate package per design.md D1a, repo packaging/path setup for both harnesses
Test: python -m pytest security-harness/workers/tests (substrate unit tests)

- [x] 1.1 Resolve design.md **D1a**: choose home (A) top-level package vs (B) canonical-copy-imported, confirming both harnesses' worker bootstrap and test `conftest`/`sys.path` can import it. Record the decision and why in design.md.
- [x] 1.2 Identify the exact **generic** surface to move: the `_LocalBackend`/`_S3Backend`/`backend_from_env` backends, `post_fact`, `read_facts`, `put_artifact`/`get_artifact`, `bump_budget`/`read_budget`, `send_message`/`read_inbox`, and a replicated `_safe` sanitizer (so the lib does not depend on `common.memory`). What stays in security-harness: `assemble_state`/`campaign_rollup`/`replay_ref` (finding-shaped read-model), `FACT_KINDS`/`RETRY_MAX`, `swarm_standup` scoring, `oracle_router`, attempt-lesson shapes. Provide a `configure(...)` seam for env-var names + default-dir so security keeps its `SC_BLACKBOARD_*`/`memory.state_dir` behavior byte-identical.
- [x] 1.3 Move the generic surface into the shared library with its unit tests; keep the append-only, per-author-unique key contract (`facts/{cid}/{kind}/{author}__{uuid}.json`) byte-for-byte.
- [x] 1.4 Confirm the moved substrate's own tests pass from the new home.

## 2. Re-point security-harness at the shared library with no behavior change

Files: security-harness/workers/common/blackboard.py (re-export/shim or import rewrite), security-harness/workers/swarm/tasks.py
Test: python -m pytest security-harness/workers/tests

- [x] 2.1 Replace security-harness's local generic implementations with imports from the shared library (a thin re-export shim at the old import path is acceptable if it avoids churn, as long as nothing is duplicated).
- [x] 2.2 Verify the security-specific logic (`swarm_standup`, `oracle_router`, attempt-lesson shapes) is untouched and still local.
- [x] 2.3 Run the full security-harness test suite; confirm green with no behavior change. (If a live swarm smoke run is feasible, confirm it produces an equivalent result; otherwise record that the unit suite is the regression gate.)

## 3. Wire coding-harness to the shared library

Files: coding-harness/workers/main.py, coding-harness/workers/common/__init__.py (or path setup), coding-harness/pyproject.toml
Test: python -m pytest coding-harness/workers/tests/test_workflows.py

- [x] 3.1 Add the shared library as an importable dependency of the coding-harness workers (packaging/path per 1.1). It is unused by any workflow yet.
- [x] 3.2 Add a minimal import-smoke test proving coding-harness workers can `import` the substrate and round-trip a fact (`post_fact` → `read_facts`) against a local-dir backend.

## 4. Real-cost pricing + aggregate per-run spend ceiling

Files: coding-harness/workers/common/cost.py, (new) coding-harness/workers/common/budget_gate.py, coding-harness/workers/tests/test_budget_gate.py
Test: python -m pytest coding-harness/workers/tests/test_budget_gate.py

- [x] 4.1 In a new `common/budget_gate.py` pure-logic module, implement cumulative real-cost accounting over the substrate's `bump_budget`/`read_budget` keys, and a decision function `gate(spend, ceiling, warnThreshold) -> {state: ok|warn|halt, report}`. No JQ; pure Python with a thin `@worker_task` wrapper.
- [x] 4.2 Define a truthful cost report shape (per-backend/per-author spend, total, ceiling, % consumed) assembled from the fact log.
- [x] 4.3 Ensure the halt is triggered by **accumulated spend only** — grep the module to prove no `timeout`/`asyncio.wait_for`/`--max-time`/`--connect-timeout` or wall-clock deadline is introduced (`AGENTS.md` timeout policy).
- [x] 4.4 Unit-test the gate: `ok` below warn, `warn` between warn and ceiling, `halt` at/over ceiling, and a truthful report at each state.

## 5. Budget gate enforcement + not-startable rule

Files: (new) coding-harness/workers/workflows/taskdefs/budget_gate_check.json, coding-harness/workers/common/budget_gate.py, coding-harness/workers/tests/test_workflows.py
Test: python -m pytest coding-harness/workers/tests/test_workflows.py

- [x] 5.1 Register a `budget_gate_check` SIMPLE taskdef (v1) matching existing taskdef shape.
- [x] 5.2 Implement the **not-startable** rule: a swarm-mode run with no enforceable ceiling refuses to start (single-agent `code_parallel` is unaffected and keeps its per-agent caps).
- [x] 5.3 Define the clean-halt behavior: on `halt`, dispatch no new work; let in-flight work reach a durable boundary; surface the cost report. (Wiring into the team cycle is Phase 2; here it is validated at the task level.)
- [x] 5.4 Confirm SIMPLE-task/taskdef/registration-parity checks pass for the new task name.

## 6. Frozen-contract artifact + contract-as-controlled-event

Files: (new) coding-harness/workers/common/contract_freeze.py, coding-harness/workers/tests/test_contract_freeze.py
Test: python -m pytest coding-harness/workers/tests/test_contract_freeze.py

- [x] 6.1 Define the concrete contract artifact for the spike (recommended: an interface/signature file + the shared tests that pin it), committed to the shared branch and readable by agents from disk.
- [x] 6.2 In `common/contract_freeze.py`, implement the "contract is fixed" check: detect when a candidate diff edits the frozen contract and classify that as a **controlled event** (its own board item) rather than an ambient edit.
- [x] 6.3 Unit-test: a diff that respects the contract passes; a diff that silently edits it is flagged as a controlled event requiring re-plan.

## 7. Shared-file ownership on the board

Files: (new) coding-harness/workers/common/file_ownership.py, coding-harness/workers/tests/test_file_ownership.py
Test: python -m pytest coding-harness/workers/tests/test_file_ownership.py

- [x] 7.1 In `common/file_ownership.py`, implement ownership as substrate facts (`post_fact` kind `ownership`), folded by a pure `fold_owner_map` into a current owner-map (not security's `assemble_state`, which stays security-specific). Pure logic; the thin `@worker_task` wrapper lands with group 9 when a workflow wires it (avoid a speculative unwired task). No JQ.
- [x] 7.2 Implement the grant rule: an item may be dispatched against a shared file only if no live owner holds it; otherwise it waits. Lock-free (per-author-unique keys), consistent with the substrate's no-lost-update guarantee.
- [x] 7.3 Unit-test: concurrent claims on the same file resolve to exactly one live owner; release makes the file grantable again.

## 8. Continuous-rebase integrator

Files: (new) coding-harness/workers/common/continuous_integrate.py, coding-harness/workers/common/git.py, coding-harness/workers/tests/test_continuous_integrate.py
Test: python -m pytest coding-harness/workers/tests/test_continuous_integrate.py

- [x] 8.1 In `common/continuous_integrate.py`, implement continuous rebase: rebase each ready short-lived worktree onto the current shared-branch head, rather than a big-bang merge at wave end. Reuse `merge_remediation` conflict logic where it fits; use `common/exec.py` for all git child processes (`AGENTS.md` runtime policy).
- [x] 8.2 On conflict, re-open the item with the precise conflict detail attached (board fact), never a silent drop.
- [x] 8.3 Unit-test: three overlapping worktrees rebased sequentially onto a moving head converge with no big-bang merge step; an unresolved conflict re-opens its item with a named mechanism.

## Status (implementation progress)

**Done and green** (verified by the test suites named per group): Phase 0 groups 1–5 (shared `swarm_substrate` library + security-harness re-pointed with no behavior change + coding-harness wired + budget gate) and Phase 1 groups 6–8 (contract-freeze, file-ownership, continuous-rebase integrator — the last with a real-git proof that overlapping worktrees converge onto a moving head with no big-bang merge). `coding-harness` worker suite: 1316 passed, 3 skipped. `security-harness` blackboard + allocator + chaining: 42 passed unchanged.

**Groups 9–10 — DONE and live-validated (2026-11-05).** Group 9 shipped `convergence_spike` v1 (a new ownership-serialized overlap fan-out; `code_parallel`'s disjoint fan-out was not reusable per design.md Decision 3) with 3 new SIMPLE tasks + workers in `campaign/tasks.py`; structural + input-docs tests green (287 passed) and 4 new worker tests. Group 10 ran the pre-registered GO/NO-GO on a forced-overlap repo via **real Conductor executions** (`feedback_run_full_harness`, never a bespoke script): **GO** — convergence_spike `93ccaa36-…` converged 3/3 overlapping forks onto a green shared branch by continuous rebase (zero big-bang merge), independently re-verified; the file-disjoint baseline `code_parallel` `2f6558d6-…` could not represent the overlap and ended `implementation_unavailable` with a big-bang-merge conflict. Write-up: `coding-harness/docs/convergence_spike_result.md`; roadmap §6 updated; Phase 2 unblocked.

**`budget_gate_check` is wired** into `convergence_spike`, and the two wiring gaps the first run surfaced are now **CLOSED and live-proven** (2026-11-05). `convergence_spike` was evolved into an ownership-gated **wave loop** (`DO_WHILE` reusing `budget_gate_check`; zero new taskdefs): (a) shared-file ownership now **gates dispatch** — clean isolated run `31fba11b-bfba-4e1f-b2b8-7016a73e0515` serialized the all-contended task into 3 waves of one agent each and still reached **GO, 3/3** (independently re-verified); (b) each wave records real agent spend, so the budget gate's **halt-on-accumulated-spend path is live** — clean isolated run `f40c0393-c621-4740-9117-1410773353d2` (ceiling `$0.01`) halted after `$0.1177` with `dispatchNewWork=false`, leaving 2 items undispatched → honest **NO-GO** naming the mechanism. The integrator also now **detects empty/failed deliveries** (a no-op fork is never mis-counted as integrated) and **re-queues transient failures — empty delivery, rebase conflict — across waves** up to a `maxAttempts` cap; only a frozen-contract edit or an exhausted retry budget is a terminal reopen. Live-proven by run `28b232cf-f315-45b7-89b7-d753c60adf3a` (a deliberate no-op fork was re-queued then terminally reopened → honest NO-GO naming the mechanism; ownership gating ran the two disjoint items concurrently and serialized only the contended one), with a GO regression run `85f15216-1cfe-4501-b301-f52bf5296201` confirming no regression. New unit coverage in `test_convergence_spike.py`; full suite 1333 passed, 3 skipped; invariant audit clean. Remaining Phase 2 work: re-plan of frozen-contract controlled-events, mid-run peer review, the durable team cycle.

## 9. Assemble the runnable convergence spike (reuse-heavy, v1)

Files: (new) coding-harness/workers/workflows/convergence_spike.json, coding-harness/workers/workflows/taskdefs/*.json, coding-harness/docs/workflow-inputs.md
Test: python -m pytest coding-harness/workers/tests/test_workflows.py coding-harness/workers/tests/test_workflow_input_docs.py

- [x] 9.1 Compose a **version 1** `convergence_spike` workflow that: reads the frozen contract (6), fans out ≥3 implementer agents (reuse `code_subtask` bodies) under file-ownership grants (7), integrates continuously (8), and runs `test_cycle` (unchanged, honest verdict) as the green gate. No JQ; no timeouts; artifacts on branch. (`workers/workflows/convergence_spike.json`.)
- [x] 9.2 Register any new taskdefs; confirm registration-parity and input-docs tests pass. (3 new taskdefs: `convergence_build_forks`, `convergence_integrate`, `convergence_outcome`; `test_workflows.py` + `test_workflow_input_docs.py` green = 287 passed.)
- [x] 9.3 Keep it minimal — reuse existing sub-workflows; justify by name any genuinely new task (per simplicity policy). Reuses `workspace_prepare`, `budget_gate_check`, `code_subtask`, `test_cycle`. Three new SIMPLE tasks are justified by name: `convergence_build_forks` (overlapping fan-out + ownership claim + contract fact — `code_parallel_build_forks` assumes file-disjoint and does a big-bang fan-in, so it is not reusable), `convergence_integrate` (continuous-rebase integrator + contract-controlled-event routing — no existing task integrates without a big-bang merge), `convergence_outcome` (the pre-registered GO/NO-GO fold). Workers live in `campaign/tasks.py` (already in the default module set) to avoid a new worker package.

## 10. Run the experiment and record the honest GO/NO-GO

Files: (new) coding-harness/docs/convergence_spike_result.md
Test: n/a (evidence from a real Conductor execution; the result doc cites the execution id)

- [x] 10.1 Construct the forced-overlap task per design.md Decision 4 (≥3 items must touch the same file(s)); review it to confirm it is genuinely not file-disjoint before running. (Target repo: `scratchpad/convergence-target`, baseline `a090a66` — three items all edit the shared `registry.py`; frozen contract = `contract.py` + `test_registry.py`; baseline is red by construction.)
- [x] 10.2 Run the baseline (current file-disjoint path) and the `convergence_spike` on the same task via real Conductor executions. (`convergence_spike` = `93ccaa36-7177-4bcb-949c-4b6b94b3a447`; baseline `code_parallel` = `2f6558d6-e505-484a-9948-4d20b380322f`.)
- [x] 10.3 Record the result against the **pre-registered** metric (Decision 4): **GO** — 3/3 overlapping forks converged onto one green shared branch by continuous rebase, zero big-bang merge, zero reopens, frozen tests pass (independently re-verified). Baseline could not represent the overlap (auto-disjointed the plan) and ended `implementation_unavailable` with a big-bang-merge conflict. Full write-up with execution ids: `coding-harness/docs/convergence_spike_result.md`. Neither outcome massaged.
- [x] 10.4 Write the roadmap recommendation: **GO → proceed to Phase 2.** Updated `SDLC_ROADMAP.md` §6 Phase 1 with the result and the two deferred wiring gaps (ownership-gated dispatch; spend recording into the budget gate).
