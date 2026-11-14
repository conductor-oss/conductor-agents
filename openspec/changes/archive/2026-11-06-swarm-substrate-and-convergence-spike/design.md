# Design

Companion to the roadmap ([`coding-harness/docs/SDLC_ROADMAP.md`](../../../coding-harness/docs/SDLC_ROADMAP.md) §4.7 H1/H4/H5, §6 Phase 0/1). This change is the **foundation layer**: shared substrate + budget gate (Phase 0) and the premise spike (Phase 1). It is scoped to leave the harness strictly more capable (Phase 0) and to produce a decisive GO/NO-GO (Phase 1), nothing more.

## Decision 1 — Lift the generic substrate; do not fork it

`security-harness/workers/common/blackboard.py` is the proven substrate, and its core comment is load-bearing: *"No two agents ever write the same key, so writes never need a lock and can never lose an update."* Keys are `facts/{cid}/{kind}/{author}__{uuid}.json` (append-only, per-author-unique). That property is harness-agnostic and is exactly what the construction swarm needs.

**What moves** (generic — append-log mechanics with no finding/vuln semantics): the local-dir and S3 backends (`_LocalBackend`, `_S3Backend`, `backend_from_env`), the fact store (`post_fact`, `read_facts`), the artifact store (`put_artifact`/`get_artifact`), the budget key family (`bump_budget`/`read_budget`), and the message bus (`send_message`/`read_inbox`). The trivial `_safe` key-sanitizer is replicated in the shared lib (identical regex to `common.memory._safe`) so the lib has **no dependency on security's `common.memory`**.

**What stays** in `security-harness` (security-specific — confirmed during implementation, corrected from the proposal's first list): `assemble_state`, `campaign_rollup`, and `replay_ref` (they encode hypothesis/attempt/finding/severity/refuter semantics — not generic), the `FACT_KINDS`/`RETRY_MAX` constants, `swarm_standup` credit-scoring, `oracle_router`, and the attempt-lesson *shapes*. Construction rebuilds its own read-model and standup/lesson shapes later (Phase 2), reusing the generic store underneath.

**Configuration seam (keeps security behavior byte-identical).** `backend_from_env` today reads security-prefixed env names (`SC_BLACKBOARD_S3_BUCKET`/`SC_BLACKBOARD_S3_PREFIX`/`SC_BLACKBOARD_DIR`) and falls back to `memory.state_dir()`. The shared lib exposes a `configure(...)` seam (module-level, per-process) for the env-var names and the default-dir provider; `security-harness/workers/common/blackboard.py` becomes a thin facade that calls `configure(...)` with exactly those `SC_` names and `memory.state_dir`, then re-exports the generic primitives and keeps its security-specific read-model local. Because the two harnesses run as separate worker processes, per-process module config is safe.

**Why not a thin "copy into coding-harness":** two divergent substrates is the outcome the roadmap explicitly rejects (one shared-state abstraction, §4.6). A single library keeps the concurrency/durability/audit guarantees in one place.

### Open question D1a — where the shared library lives (resolve in Task 1)

Both harnesses today import `from common import ...` with `common` rooted at each harness's own `workers/` (`coding-harness/workers/main.py:35`). `security-harness` has no `pyproject.toml`; `coding-harness` does (`conductor-coding-harness`). Candidates, to be decided by the implementer with the simplicity policy in force:

- **(A) New top-level package** (e.g. `swarm-substrate/` at repo root) that both harnesses add to their worker `sys.path` / packaging. Cleanest ownership, one home, but touches both harnesses' path setup.
- **(B) Canonical copy in one harness, imported by the other** via an explicit path entry. Less new scaffolding, but asymmetric ownership and a cross-harness path dependency.

Recommendation: **(A)**, because the library is co-owned and neither harness should be "the owner." The implementer must confirm both harnesses' worker bootstrap and test `conftest`/`sys.path` can resolve it, and that `security-harness`'s existing tests still pass unchanged. This is a research+decide task, not a guess to bake into the proposal.

## Decision 2 — Budget gate is a spend ceiling, not a deadline, and makes no up-front estimate

`AGENTS.md`'s timeout policy forbids execution deadlines but explicitly exempts spend/turn caps ("agent limits, not wall-clock deadlines"). The gate is therefore a **cumulative-spend** ceiling:

- **Pricing:** derive real cost from token usage per backend (build on `coding-harness/workers/common/cost.py`; the generic `bump_budget`/`read_budget` keys from Decision 1 accumulate per-author spend lock-free).
- **Ceiling + early warning (H5):** the run carries a configured ceiling; an early-warning threshold below it surfaces a "approaching ceiling" signal; at the ceiling the run **halts cleanly** (no new work dispatched, in-flight work allowed to reach a durable boundary) and emits a **truthful cost report**. 
- **No false precision:** the gate does **not** predict "this scope ≈ $X." Rework depth is unknowable up front (H5); the human approves an order-of-magnitude ceiling and the run proves or disproves it.
- **Not-startable rule:** a swarm-mode run with no enforceable ceiling must refuse to start. (Single-agent `code_parallel` is unaffected — it keeps its per-agent caps.)
- **No timeout smell:** the halt is triggered by accumulated spend, never by elapsed wall-clock; no `asyncio.wait_for`, `timeout`, `--max-time`, etc. is introduced.

## Decision 3 — The Phase 1 spike builds real primitives, not throwaway scaffolding

A spike is tempting to throw away, but the roadmap's H1/H4 response names three mechanisms Phase 2 will need regardless. Building them *as the spike* keeps the simplicity policy honest (nothing speculative) and means a GO result hands Phase 2 working primitives:

1. **Frozen contract artifact (H4).** A concrete committed artifact (recommended: an interface/signature file plus the shared tests that pin it) that participating agents are instructed to treat as fixed. A change to it is a **controlled event** — its own board item that re-plans dependents — never an ambient edit. The spike's contract is authored by hand (no Architect role is built here).
2. **Shared-file ownership on the board (H1).** Ownership of a shared file is a fact on the Phase 0 blackboard (`post_fact` kind `ownership`, folded by `assemble_state`). The dispatcher grants an item a shared file only if no live owner holds it; two agents never hold the same file open concurrently.
3. **Continuous re-sync + continuous-rebase integrator (H1).** Agents work in **short-lived** worktrees and re-sync to the shared branch frequently; the integrator rebases each ready worktree onto the current shared head continuously, rather than `merge_worktrees`' big-bang merge at wave end. On conflict it re-opens the item with the conflict detail (reusing `merge_remediation` logic where it fits).

**Reuse over new:** the spike reuses `code_subtask` for the agent bodies and `test_cycle` for the green gate (unchanged, honest outcomes). The genuinely new pieces are the ownership record, the continuous-rebase integration strategy, and the contract-as-controlled-event discipline. Any new workflow is **version 1** only; no JQ is added (ownership/integration logic lives in a `common/` pure-logic module with a thin `@worker_task` wrapper, per `AGENTS.md`).

**Discovered constraint (recorded during implementation).** `code_parallel`'s existing fan-out path is **not** directly reusable for the spike: `code_parallel_build_forks` partitions work into **file-disjoint** subtasks and `merge_worktrees` does a big-bang fan-in — both assume non-overlapping files, which is the exact opposite of the spike's premise (≥3 items touching the *same* file). So group 9's workflow is a **new ownership-serialized fan-out** (assign overlapping files, gate dispatch on `file_ownership`, integrate with `continuous_integrate` instead of `merge_worktrees`), not a thin wrapper over `code_parallel`. This new fan-out engine overlaps substantially with Phase 2's team cycle and, like the GO/NO-GO run itself (group 10), must be built and validated against the **live worker fleet** (agent backends + a target repo) rather than authored blind — authoring a multi-sub-workflow Conductor definition without iterating against a live server/fleet produces something that may pass structural checks yet not actually run, which would be a false "done."

## Decision 4 — The experiment protocol and the GO/NO-GO bar

The spike is an **experiment with a pre-registered success metric**, so the result cannot be rationalized after the fact.

- **Task selection:** a small, real change on a small repo, deliberately constructed so that **≥3 work items must touch the same file(s)** (e.g. add three fields that all route through one shared schema/dispatcher). File-disjoint tasks are disqualified — they prove nothing about H1.
- **Team:** exactly ≥3 concurrent implementer agents on the overlapping work, behind the frozen contract.
- **Baseline:** compare against the current file-disjoint campaign path on the same task (which is expected to either serialize or collide), so "the new model helped" is measured, not asserted.
- **Success (GO):** all agents' work lands on one shared branch assembled by **continuous re-sync** (zero big-bang merge step), the branch is **green** under `test_cycle` (honest verdict), and a reviewer pass finds the shared-code changes coherent.
- **Failure (NO-GO):** if convergence cannot be reached, the result is a **documented finding** naming the precise failure mechanism (per `AGENTS.md` terminology policy — e.g. "continuous rebase thrashes because contract X was under-specified in dimension Y"), and the roadmap recommendation becomes: scope the swarm to file-disjoint work, do not build Phase 2's shared-code path.
- **Honesty:** neither outcome is massaged. A green that required a hidden big-bang merge, or tests weakened to pass, is a NO-GO.

## Decision 5 — Scope fence

Not built here (later phases, gated on GO): the standup→fan-out→integrate→learn→re-plan cycle, boards as a durable product, monitor agents, dynamic team sizing, hierarchy, `sdlc_intake`, `bug_fix`, `project_bootstrap`, `cc_program`, `repo_memory`, TUI board-awareness, and the web console. No DevOps in any phase (roadmap §8). This change is foundation + proof only.

## Risks

- **Substrate lift breaks security-harness.** Mitigated by: no behavior change requirement + its existing test suite as the regression gate; the lift is a move+re-point, not a rewrite.
- **Packaging boundary (D1a) is thornier than expected.** Mitigated by making it an explicit research/decide task with a fallback (B) if (A) proves invasive.
- **The spike's task is accidentally file-disjoint and proves nothing.** Mitigated by Decision 4's disqualification rule and the forced-overlap construction, reviewed before the run.
- **A NO-GO feels like failure and tempts massaging.** Mitigated by pre-registering the metric here: a documented NO-GO is a *successful* outcome of this change — it saves building Phase 2 on a false premise.
