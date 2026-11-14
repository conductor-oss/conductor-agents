## Why

[`coding-harness/docs/SDLC_ROADMAP.md`](../../../coding-harness/docs/SDLC_ROADMAP.md) sets out to grow the coding harness from a one-shot coder into a collaborating **team of agents** (1→hundreds, runs lasting minutes→days) that build software on shared boards. The roadmap is explicit that most of that vision is unbuildable until two things are settled first, and that building the team cycle before settling them would be building on an unproven premise:

- **The team substrate does not yet exist as a shared thing.** The only working swarm substrate in this repo lives inside `security-harness` (`workers/common/blackboard.py` and siblings — the append-only fact store, the `inbox`/`send` message bus, dedup, cross-run learning). The coding harness has none of it. Forking a second copy would create two divergent substrates to maintain; the roadmap's Phase 0 says lift the **generic** parts into a shared library both harnesses import, and leave the security-specific logic where it is.
- **A swarm that cannot stop itself on spend must not be startable.** Hundreds of agents over days multiplies cost by orders of magnitude, and per-run budget enforcement (`coding-harness/workers/IMPROVEMENTS.md` §3.3) is still unbuilt. The existing caps are **per-agent** turn/dollar limits; there is no **aggregate per-run ceiling**. Roadmap H5 makes a hard budget gate a Phase 0/1 prerequisite, not a later monitor feature.
- **The central technical premise is unproven.** The whole team model rests on many agents writing to **one shared, mutable codebase** and converging. Today's parallelism is file-**disjoint** (the campaign scheduler only runs non-overlapping work); the moment two items touch shared code (a router, a schema, an API contract) the disjoint model collapses, and N long-lived branches merged pairwise is exactly the exponential merge pain real teams avoid. Roadmap H1/H4 call this the first spike and a **GO/NO-GO**: if concurrent agents cannot converge on shared code behind a frozen contract with continuous re-sync, the swarm premise is wrong for anything but file-disjoint work, and the honest outcome is to scope it there — **before** Phase 2 is built.

This change delivers exactly those prerequisites (roadmap Phase 0) and runs the premise spike (roadmap Phase 1). It deliberately does **not** build the team cycle, boards-as-product, monitors, intake, or any UI — those are later phases, gated on this one's GO result.

## What Changes

**Phase 0 — shared substrate (lift, do not fork).**
- Extract the **generic** swarm substrate out of `security-harness/workers/common/` into a shared library both harnesses import: the append-only blackboard fact store (`post_fact`/`read_facts`), the artifact store (`put_artifact`/`get_artifact`), the budget key family (`bump_budget`/`read_budget`), the local-dir and S3 backends, and the `send_message`/`read_inbox` message bus. The **security-specific** logic — the `assemble_state`/`campaign_rollup`/`replay_ref` read-model, the `FACT_KINDS`/`RETRY_MAX` constants, `swarm_standup` credit-scoring, `oracle_router`, and attempt-lesson *shapes* — stays in `security-harness` and is not moved.
- `security-harness` is refactored to import the extracted library in place of its local copies, with **no behavior change** to its swarm.
- `coding-harness` gains the same library as a dependency, unused by any workflow yet (it is consumed by Phase 1 and later phases).

**Phase 0 — hard per-run budget gate.**
- Add real-cost pricing and an **aggregate per-run spend ceiling** that halts a run cleanly when cumulative spend reaches a configured ceiling, with a truthful cost report. This is a **spend** limit, not a wall-clock deadline (per `AGENTS.md`'s timeout policy: turn/dollar caps are agent limits, not execution deadlines). It adds burn-rate visibility and an early-warning threshold below the ceiling; it does not predict cost up front (roadmap H5 forbids false up-front estimates — gate on a ceiling the run proves or disproves as it goes).

**Phase 1 — the H1/H4 convergence spike (GO/NO-GO).**
- Build the minimal, reusable integration primitives the roadmap names as the response to H1/H4, and run a controlled experiment with them:
  1. **Contract freeze first (H4):** a concrete, committed contract artifact that participating agents treat as fixed; a change to it is a controlled event, never an ambient edit.
  2. **Shared-file ownership on the board (H1):** which agent holds which shared file is a record on the (Phase 0) blackboard, so two agents never hold the same file open concurrently.
  3. **Short-lived worktrees with continuous re-sync + a continuous-rebase integrator (H1):** agents re-sync to the shared branch frequently and the integrator rebases continuously, rather than a big-bang merge at the end.
- Run the experiment on a task **deliberately chosen to force overlap** and record an **honest** result: either ≥3 agents concurrently modify overlapping code and converge to a green, reviewed branch **without a big-bang merge**, or a documented finding that they cannot and precisely why. "Cannot converge" is a valid, first-class outcome that scopes the swarm to file-disjoint work and stops Phase 2.

## Capabilities

### New Capabilities
- `harness-swarm-substrate`: The generic, harness-agnostic swarm substrate — append-only blackboard fact store, `inbox`/`send` message bus, dedup, and cross-run-learning plumbing — lives in one shared library that both `coding-harness` and `security-harness` import; the security-specific orchestration logic stays in `security-harness`.
- `harness-budget-gate`: A run carries an aggregate spend ceiling; cumulative real-cost spend is tracked and surfaced, an early-warning threshold fires below the ceiling, and the run halts cleanly with a truthful cost report when the ceiling is reached. A run with no enforceable ceiling is not startable in swarm mode.
- `harness-shared-code-convergence`: The integration model that lets concurrent agents write to one shared codebase — a frozen contract artifact, shared-file ownership recorded on the board, short-lived worktrees with continuous re-sync, and a continuous-rebase integrator — proven (or disproven) by a controlled GO/NO-GO experiment whose outcome is reported honestly.

### Modified Capabilities
_None. `security-harness`'s swarm behavior is explicitly unchanged by the substrate lift; this change adds its result as a requirement of `harness-swarm-substrate` rather than modifying a security-harness capability (security-harness has no OpenSpec specs in this store)._

## Impact

- **New shared library:** a harness-agnostic package (home decided in `design.md`) holding the extracted generic substrate; imported by both harnesses.
- **security-harness:** `workers/common/blackboard.py` and the sibling generic modules are re-pointed at the shared library; the security-specific swarm tasks (`workers/swarm/tasks.py`) are unchanged in behavior. Its existing swarm run must produce the same result after the lift (regression-checked).
- **coding-harness:** depends on the shared library; gains the budget-gate worker/pricing (building on `workers/common/cost.py`) and the Phase 1 convergence primitives (contract freeze, ownership record, continuous-rebase integrator). Honors `AGENTS.md`: one workflow version, zero new JQ tasks, no timeouts, simplicity-justified additions, honest `test_cycle` outcomes.
- **Behavior:** after this change the harness has a shared swarm substrate and a hard spend ceiling, and the project has a documented, evidence-backed answer to "can concurrent agents converge on shared code?" — the GO/NO-GO that governs whether Phase 2 (the team cycle) is built at all.
- **Explicitly not in scope:** the team cycle / boards-as-product, monitor agents, `sdlc_intake`, `bug_fix`, `project_bootstrap`, `cc_program`, `repo_memory`, and any TUI/web console. No DevOps of any kind (roadmap §8).
