# Convergence Spike — GO/NO-GO Result (Roadmap Phase 1, H1/H4)

**Verdict: GO.** Three agents modified the *same* shared file behind a frozen contract and
converged to a green, coherent shared branch assembled by continuous rebase — with **zero big-bang
merge**. The result below is from a real Conductor execution, independently re-verified by hand.

This is the decisive experiment pre-registered in
the `swarm-substrate-and-convergence-spike` OpenSpec change
Decision 4. Neither outcome was massaged; the bar and the measurement were fixed before the run.

## The pre-registered bar (design.md Decision 4)

- **Task:** a small change deliberately constructed so **≥3 work items must touch the same file(s)**
  — file-disjoint tasks are disqualified because they prove nothing about H1.
- **Team:** exactly ≥3 concurrent implementer agents on the overlapping work, behind a frozen contract.
- **GO:** all agents' work lands on one shared branch by **continuous re-sync (zero big-bang merge)**,
  the branch is **green** under `test_cycle` (honest verdict), and the shared-code changes are coherent.
- **NO-GO:** convergence cannot be reached → a documented finding naming the precise failure mechanism,
  and the recommendation becomes "scope the swarm to file-disjoint work; do not build Phase 2's
  shared-code path."
- **Honesty:** a green that required a hidden big-bang merge, or tests weakened to pass, is a NO-GO.

## The forced-overlap task

A tiny Python repo (`scratchpad/convergence-target`, baseline commit `a090a66`) with a genuine
shared-write hotspot:

- **Frozen contract** (agents must build against, never edit): `contract.py` (the `Handler`
  signature) and `test_registry.py` (three tests pinning `dispatch("alpha"|"beta"|"gamma", …)`).
- **Shared file:** `registry.py` — a `HANDLERS` dict + `register()`/`dispatch()`. **All three work
  items must edit this one file** to register their handler.
- Three work items (`alpha`, `beta`, `gamma`), each: create `handler_<x>.py` **and** register it in
  `registry.py`. Files per item: `["registry.py", "handler_<x>.py"]` — `registry.py` overlaps across
  all three.
- Baseline state is **red by construction**: at `a090a66` no feature is registered, so the three
  frozen tests fail with `KeyError`. Convergence is proven only when the shared branch turns green.

## The convergence_spike run (the new model)

- **Workflow:** `convergence_spike` v1 — `workspace_prepare → budget_gate_check → (startable SWITCH)
  → convergence_build_forks → FORK_JOIN_DYNAMIC of 3 × code_subtask → convergence_integrate →
  test_cycle → convergence_outcome`. Reuses `code_subtask` for the agent bodies and `test_cycle`
  (unchanged, honest verdict) for the green gate.
- **Execution id:** `93ccaa36-7177-4bcb-949c-4b6b94b3a447` — status **COMPLETED**. Reproduced on the
  shipped definition by a second clean run, `977331df-bdf6-41e0-8e28-e5914ef4dd4e` — also GO, 3/3
  converged, zero big-bang merge, zero reopens, green.
- **Outcome (workflow output):**
  - `verdict: GO`, `converged: true`
  - `integratedCount: 3 / forkCount: 3`, `reopenedCount: 0`
  - `bigBangMerge: false`
  - `testCycleState: tests_passed`, `testsPassed: true`
  - shared branch `convergence-spike-93ccaa36` @ `09234a1a5e80…`

### Mechanisms exercised, confirmed on the live blackboard

Facts for the run (`coding-harness/workers/state/blackboard/facts/93ccaa36-…/`):

- **Contract freeze (H4):** 1 `contract` fact recording `paths=[contract.py, test_registry.py],
  frozen=true`. `convergence_integrate` classified each fork's diff against these; none touched the
  contract, so there were **0 controlled events** — all three were ordinary, integration-eligible.
- **Shared-file ownership (H1):** 6 `ownership` claim facts — each agent claimed `registry.py` and
  its own handler. The pure fold resolves `registry.py` to exactly one live owner (`alpha`, the first
  claimant) with `beta`/`gamma` queued, demonstrating the lock-free single-owner guarantee.
- **Continuous-rebase integrator (H1):** the shared branch history is **linear with zero merge
  commits** — `baseline → alpha → beta → gamma` (interleaved with `test_cycle`'s agent-authored
  tests), each fork rebased onto the moving shared head. `0` reopen facts.

### Independent re-verification (not just the harness's own report)

- `git log --merges a090a66..09234a1` → **empty** (no big-bang merge commit anywhere).
- `registry.py` at the shared head contains **all three** registrations, each under its own marker,
  coherent and non-duplicated — assembled from three concurrent agents editing the one file.
- `contract.py` and `test_registry.py` are **unchanged** from baseline.
- Checking out `09234a1` in a clean worktree and running the **frozen** `test_registry.py` directly:
  **3 passed**. The green is real.

## The baseline (the current file-disjoint model) — contrast

The current file-disjoint path is `code_parallel`. Run on the **same** overlapping task
(`usePrecomputedPlan` with the identical three items, each listing `registry.py` as a shared file):

- **Execution id:** `2f6558d6-e505-484a-9948-4d20b380322f` — status COMPLETED.
- **It could not express the overlap.** `code_parallel`'s plan validator rejects a plan where one
  file belongs to more than one subtask, so its autonomous **plan-repair agent rewrote the task**:
  it stripped `registry.py` out of `alpha`/`beta`/`gamma` (leaving each only its own handler) and
  invented a **separate fourth subtask `wire-registry` owning `registry.py` alone**. The file-disjoint
  model's only way to handle a shared file is to serialize it to a single owner — exactly the
  limitation H1 predicts.
- **And it still failed to deliver.** Final verdict: `verified: false`,
  `verificationState: implementation_unavailable`, `deliveryOutcome: implementation_unavailable`. The
  three feature forks finished `COMPLETED_WITH_ERRORS` (their original "register in `registry.py`"
  instruction no longer matched their reduced file scope), and the **big-bang merge conflicted** on
  the isolated shared file: `conflicts: ["cc-group-wire-registry"]` (`alpha`/`beta`/`gamma` merged,
  `wire-registry` did not). A top-level `testCycleState: tests_passed` is not a delivery: it ran
  against a merge candidate that `verified: false` / `implementation_unavailable` already disqualifies.

**Measured contrast (same task, same agents, same repo):**

| | `convergence_spike` (new) | `code_parallel` (baseline, file-disjoint) |
|---|---|---|
| Kept the overlap (3 items share `registry.py`) | yes | no — auto-repaired to disjoint + a lone 4th subtask |
| Assembly | continuous rebase, **zero big-bang merge** | big-bang `merge_worktrees` |
| Shared-file outcome | coherent, all 3 registrations present | **merge conflict** on `wire-registry` |
| Delivery verdict | `GO` / converged, branch green | `implementation_unavailable`, `verified: false` |

So "the new model helped" is measured, not asserted: on an overlap the file-disjoint path cannot even
represent without rewriting the task, the convergence model converged to a verified-green branch.

## Gap closure — ownership-gated dispatch + live budget halt (hardened engine)

The first GO above ran a **single concurrent wave** and leaned on continuous rebase to converge; two
mechanisms were recorded but not yet enforced. Both are now **closed and live-proven**. `convergence_spike`
was evolved into an ownership-gated **wave loop** (a `DO_WHILE`): each wave `convergence_build_forks`
dispatches only the items whose shared files are free (claiming them), `convergence_integrate` rebases
that wave, **releases** its file ownership, and **records each agent's real spend** on the board, and the
reused `budget_gate_check` decides whether the next wave may dispatch.

- **Gap 1 — ownership now GATES dispatch.** Clean isolated run `31fba11b-bfba-4e1f-b2b8-7016a73e0515`
  (ceiling `$20`): because all three items contend on `registry.py`, dispatch serialized into **3 waves
  of one agent each** (alpha → beta → gamma), two agents never holding the shared file at once — and it
  **still converged: GO, 3/3 integrated, zero reopens, zero big-bang merge, branch green**, spend
  `$0.40`. Independently re-verified: no merge commits, frozen `test_registry.py` passes (3/3) at the
  shared head. Disjoint items would still batch into one wave; only contended files serialize.
- **Gap 2 — budget halt-on-accumulated-spend is live.** Clean isolated run
  `f40c0393-c621-4740-9117-1410773353d2` (ceiling `$0.01`): wave 1 (alpha) integrated and its real spend
  `$0.1177` was recorded to the board; `budget_gate_check` then returned **`state=halt`,
  `dispatchNewWork=false`**, so the loop **stopped and beta + gamma were never dispatched** → honest
  **NO-GO** naming the mechanism: "2 item(s) never dispatched — budget gate halted the run before they
  ran (beta, gamma)." This is the halt path that `$0` spend could not previously exercise.

- **Gap 3 — failed/empty deliveries self-heal; none is mis-counted.** A dispatched fork that delivers
  **no commit** (a failed or no-op agent) is detected as `empty_delivery` and is **never counted as
  integrated** (it previously slipped through as a no-op "integrated"); together with a rebase conflict,
  it is **re-queued for a later wave** up to `maxAttempts`, and only a frozen-contract edit or an
  exhausted retry budget is a terminal reopen. Live-proven end to end by run
  `28b232cf-f315-45b7-89b7-d753c60adf3a` (gamma given a deliberate no-op task, `maxAttempts=2`):
  - wave 1 dispatched **alpha + gamma concurrently** (disjoint files) while serializing beta (shares
    `registry.py`) — ownership gating permits real concurrency and serializes only genuine contention;
  - `integrate__1`: alpha integrated, **gamma empty → re-queued (retry), not integrated, no reopen fact**;
  - wave 2: beta integrated, gamma empty again at the cap → **terminal reopen**;
  - verdict **NO-GO**, integrated 2 / reopened 1, naming the mechanism ("fork gamma produced no commit …
    not counted as integrated"). A `GO` regression run on the same engine
    (`85f15216-1cfe-4501-b301-f52bf5296201`) stayed GO, 3/3.

Unit coverage was added alongside (`test_convergence_spike.py`): ownership gating across waves, the
integrator's release + spend recording, the empty-delivery → retry → terminal-at-cap path, and the
board-read GO/NO-GO fold (GO, budget-halt NO-GO, reopen NO-GO).

## Honest caveats (what this run does and does not prove)

- **Scope:** one small, cleanly-separable overlap (three registrations with distinct insertion
  markers in one file). It proves the convergence *machinery* works end-to-end on real agents and
  real git; it does not prove convergence for arbitrarily entangled edits. A harder overlap (agents
  editing adjacent lines of the same function) can still legitimately conflict — the integrator is
  built to **reopen with the precise mechanism**, not to paper over it (see
  `test_continuous_integrate.py`, which exercises exactly that conflict path).
- **Contract controlled-events are terminal here.** A fork that edits the frozen contract is reopened
  and surfaced (correctly, as a re-plan signal) but is not itself re-planned/retried — building the
  re-plan-dependents path, mid-run peer review, and the durable team cycle is Phase 2 work. (Transient
  failures — empty delivery, rebase conflict — *are* retried across waves, see Gap 3 above.)

## Recommendation

**GO → proceed to Phase 2's shared-code path.** The three mechanisms the roadmap's H1/H4 response
named (frozen contract as a controlled event, shared-file ownership on the board, continuous-rebase
integration) are built, live, and sufficient to let ≥3 agents converge on overlapping code without a
big-bang merge — now with **ownership-gated wave dispatch** and a **live aggregate budget halt** (both
gaps above closed). Phase 2 should build the durable team cycle on top of them (standup → fan out →
review → integrate → learn → re-plan), including retry/re-plan of reopened items and mid-run peer review.
