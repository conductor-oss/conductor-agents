# Team Cycle — Phase 2 Result (collaborating construction swarm)

**Status: shipped and live-proven.** `team_build` v1 turns the proven Phase-1 convergence wave loop
into the full roadmap §4.3 team cycle — **standup → fan out → implement ∥ review → integrate → learn
→ re-plan** over a *mutable* tracking board, with a learning board, a review board, dynamic team
sizing, mid-run peer review, live lesson sharing, human steering, and graceful degradation. Proven by
real Conductor executions, independently re-verified.

## What shipped

- **The construction read model** (`workers/common/team_board.py`): the three boards (§4.2) as kinds
  on the one append-only `swarm_substrate` log — `item` / `state` (tracking), `lesson` (learning),
  `review` / `directive` (review+steering). `fold_board` reconstructs the live kanban (latest state
  per item wins), `ready_items` respects dependencies, `size_wave` is the concrete team-sizing
  formula (open question #1), and `board_rollup` is the human-digestible summary (H7). `blackboard.py`
  had explicitly deferred this read model to Phase 2.
- **The team cycle workers** (`workers/team/tasks.py`): `team_route` (H9/H12 mode routing),
  `team_plan_init` (seed board + freeze contract), `team_standup` (the Lead: fold → size → ownership-
  gated dispatch, injecting the live board + teammates' lessons + the item's directives into each
  implementer), `team_integrate` (continuous-rebase integrator with board transitions + spend +
  release), `team_review_build` / `team_apply_reviews` (mid-run peer review, H8/H13), `team_outcome`
  (honest DELIVERED/INCOMPLETE fold).
- **The `team_build` workflow** (`workers/workflows/team_build.json`): the DO_WHILE cycle, reusing
  `code_subtask` (implementers), `coding_agent` (read-only reviewers), `test_cycle` (the green gate),
  `budget_gate_check` (H5 halt), `workspace_prepare`, and — for the degrade path — `code_parallel`.
- **Board-aware TUI** (`tui/board.py`, `tui/screens/board.py`): a live board viewer + steering
  surface (§4.8). Every steering action is the same authored board write an agent makes; the human
  carries override authority (H16). Reachable from the run-detail screen (`b`).

## Live proof — `team_build` DELIVERED

**Target** (`scratchpad/team-target`, baseline `a6e4012`, red by construction): a frozen contract
(`contract.py` `Channel` protocol + `test_contract.py`) and a shared `registry.py`; three work items
(`email`, `sms`, `push`), each creating its own channel file **and** registering it in the shared
`registry.py` — so all three contend on one file, with per-item acceptance criteria.

**Execution `2501ed93-4aeb-4a69-87d2-a589a436d995` — COMPLETED, verdict DELIVERED.**

- **Dynamic team sizing + ownership-gated dispatch.** Standup sized the wave at **3** from 3 ready
  items, but shared-file ownership let only one dispatch per wave (all three contend on
  `registry.py`), serializing into **3 waves of one agent each** — two agents never held `registry.py`
  at once. (Disjoint items would batch into one wave; only contention serializes — proven in Phase 1.)
- **The full cycle ran each wave:** `standup → implement (code_subtask) → integrate (continuous
  rebase) → peer review (a read-only `coding_agent`, a *different* agent than the implementer) →
  apply/re-plan → budget gate`.
- **Live lesson sharing (agents read each other's board).** Wave 2's implementer prompt literally
  contained wave 1's lesson — `"[high] item email delivered and peer-reviewed (refs: registry.py,
  email.py)"` — folded live from the learning board. 3 lessons were shared across the run.
- **Live human steering (a human nudges one item mid-run).** While the run was executing, a human
  directive was posted to the `sms` item through the TUI board module
  (`board.nudge_item(run, "sms", …)`). Wave 3's `sms` implementer prompt then carried it under
  **"Directives you MUST honor for this item: (human) Human steering: name the class exactly
  SmsNotifier…"** — the human travelled the exact same wire as a peer, and it reached work dispatched
  after the directive was posted.
- **Honest outcome folded from the board:** `DELIVERED — all 3 work items reached done on the shared
  branch (continuous rebase, zero big-bang merge); branch green (test_cycle=tests_passed). Head
  682bcfb3. Peer-reviewed; 3 lessons shared.`

**Independent re-verification:**
- `git log --graph` is **linear**; `git log --merges` is **empty** (no big-bang merge).
- `registry.py` at the head contains **all three** registrations (`email`, `push`, `sms`), coherent.
- `contract.py` and `test_contract.py` are **unchanged** from baseline (frozen contract intact).
- The **frozen** `test_contract.py` passes **3/3** at the head (run in the worker venv).
- Board fact kinds written for the run: `contract, directive, item, lesson, ownership, review, spend,
  state` — every board exercised.

## Live proof — graceful degradation (H9)

**Execution `11d5e604-58e2-4553-b8f0-af63ca2e6294` — COMPLETED.** A single-item `team_build`
(`add multiply() to mathutils.py`) was routed by `team_route` to **mode `single` → plain
`code_parallel` → terminate**, with **no board tasks executed and zero blackboard facts written** for
the run (verified: no `facts/<runId>` directory exists). Ceremony scales with the work; the floor is
zero.

## Live proof — mid-run peer review `changes → reopen → revise → green` (full loop)

The whole peer-review loop is proven end to end with real agents (run
`cf79c731-f0c5-4729-adaa-4aa75ed2ed58`). To exercise the loop deterministically, the item carries a
**hidden `reviewCriteria`** — the reviewer's oracle, deliberately **not** shown to the implementer
(H8/H13: separate the author from the oracle so review catches what the handed-over spec and the
automated tests do not). Here: implementer sees "implement `add` to return the sum"; the reviewer also
checks a convention the implementer never saw ("`calc.py` must define `AUTHOR = 'harness'`").

- **Wave 1:** implementer delivered `add` (passes the frozen test) → a real `coding_agent` reviewer
  returned **`changes`** with the concrete finding (missing `AUTHOR`) → `team_apply_reviews` **reopened**
  the item and posted the finding as a directive.
- **Wave 2:** the re-dispatched implementer, led by the revision directive, **edited `calc.py`** (added
  `AUTHOR = "harness"`, kept `return a + b`) → reviewer returned **`lgtm`** → item **done** → **DELIVERED**.
- Independently verified: the delivered `calc.py` contains both the sum and the reviewer-required
  `AUTHOR` constant; the suite is green.

Three real defects were found and fixed closing this loop, each a latent bug the `changes` path exposed
(none a caveat — all fixed and regression-tested): (1) `coding_agent` returns its structured verdict
under `result`/`structured`, so review verdicts were parsed wrong (`_review_fields`); (2) inside the
DO_WHILE the review JOIN is keyed `review_<id>__N`, so `apply_reviews` silently missed a real `changes`
and defaulted to `lgtm` (suffix-tolerant lookup); (3) a re-dispatched agent dismissed a non-tested
directive with "tests pass, no changes needed", so the revision prompt now leads with the required
changes and forbids that dismissal.

## Live proof — scale beyond a handful (8-wide single wave)

Execution `c11cf2a5-4f52-4b99-b766-ab389c3b80eb`: a `team_build` with 8 file-disjoint items sized the
wave at **8 and dispatched all 8 implementers concurrently in one wave**, converging to **DELIVERED,
8/8, branch green** (independently verified: 8 feature modules + tests, 9 passed). This is 2×+ past the
previously demonstrated 3–4-wide ceiling. Larger still (tens→hundreds) is bounded only by the physical
substrate (worker slots + a Postgres-backed board, roadmap §4.7 H6), not by the cycle design — the
concrete next step for higher scale, proven incrementally as the roadmap requires.

## Tests

`workers/tests/test_team_board.py` (fold / standup / sizing / termination / boards),
`workers/tests/test_team_cycle.py` (routing, ownership-gated standup + injection, integrate board
transitions + spend + retry/blocked, review build + apply incl. the real coding_agent output shape,
bounded rounds, honest outcome), and `tui/tests/test_board.py` (projection + steering). Full suite
green; harness invariant audit clean (one registered version each).

## Recommendation

**Phase 2 complete → proceed to Phase 3 (monitors & steer-by-exception).** The team cycle runs a
collaborating team on shared code with dynamic sizing, live lessons, mid-run peer review, human
steering, and graceful degradation, producing a reviewed green branch. Phase 3 adds the monitor role
(stall/drift/duplication/quality/budget detection) and the web Swarm Console + push notifications so a
days-long run is driven by exception rather than by watching the raw board.
