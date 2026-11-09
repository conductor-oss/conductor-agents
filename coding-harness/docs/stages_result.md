# Stage Front Door & Bug Depth — Phase 4 Result (G1 + G2)

**Status: shipped and live-proven.** The harness now has a single front door (`sdlc_intake`, G1) that
classifies a raw request and routes it two-dimensionally, and a reproduction-first bug path
(`bug_fix`, G2) that proves a fix red-at-parent / green-at-fix and guards against oracle tampering.
Both proven by real Conductor executions.

## What shipped

- **`bug_fix` (G2)** — reproduction-first (`workers/common/bug_fix.py` + `stages/` workers +
  `workflows/bug_fix.json`): a `coding_agent` authors a **failing** regression test (no verification
  step, so it cannot "repair" the bug away), the `commit` worker commits it, the raw
  `test_discover`/`test_plan_resolve`/`test_run` confirm it is **red** at the parent commit
  (`bug_repro_gate` — a passing test is `cannot_reproduce`, an honest stop), a `code_subtask` fixes the
  source, `test_cycle` proves the branch **green**, and `bug_outcome` emits `FIXED` only if reproduced
  ∧ green ∧ **the repro test was not tampered with** (H13 — a git diff of the test between the repro
  commit and the fix head).
- **`sdlc_intake` (G1)** — single front door (`workers/common/intake.py` + `intake_route` +
  `workflows/sdlc_intake.json`): a read-only `coding_agent` classifies the request (kind, size,
  confidence, normalized instruction, decomposition, frozen-contract paths); `intake_route` folds that
  into a route — low confidence with a human available → a bounded clarify gate; otherwise by **kind**
  (`bug` → `bug_fix`, `greenfield` → project_bootstrap) and by **execution mode** (multi-item/coupled
  → `team_build`, one small piece → `code_parallel`, read off the decomposition, H9/H12). It owns no
  branch; each routed stage workflow owns its own workspace.

## Live proof — `bug_fix` FIXED (`e6fcfe0d-3c38-4ad9-a10f-911a509498da`)

Target: a `calc.py` with a latent bug (`add` subtracts), suite green at baseline. Bug report:
*"add(2, 3) gives -1 but should give 5."*

- `red_run.verificationState = failed` → `bug_repro_gate = reproduced`; `test_cycle = tests_passed` at
  the fix; `bug_outcome = FIXED`, `reproTestTampered = false`.
- **Independently re-verified** on the branch: history is `baseline → bug_fix: failing regression test
  (ad1af3c) → code_subtask fix (8ec04b6)`; the repro test **fails at the parent** (`assert -1 == 5`)
  and **passes at the fix** (2 passed); `calc.py` changed `a - b` → `a + b`; and
  `git diff ad1af3c..8ec04b6 -- test_bug_repro.py` is **empty** (the fix did not touch its own oracle).

An earlier attempt exposed a real design trap, now fixed: `code_subtask` always runs its own
self-repairing `test_cycle`, so using it for the *repro* phase let it fix the bug while "writing the
test" (the repro commit came out green). The repro phase now uses a bare `coding_agent` + `commit`
with no verification, so the failing test is committed red.

## Live proof — `sdlc_intake` routes all three ways

- **Bug → `bug_fix`** (`6667bf96-…`): classified `kind=bug` (confidence 0.98) → `route=bug` →
  `bug_fix` → **FIXED, delivered=true** (the full G1→G2 path end to end).
- **Feature → `team_build`** (`299efdca-…`): classified `kind=feature`, **2 items** → `route=team` →
  `team_build` ran. Honest outcome `INCOMPLETE` (the classifier over-decomposed a one-line feature and
  one item blocked) — the *routing* is correct; decomposition quality is the H14 concern Phase 2's
  board already surfaces, not an intake defect.
- **Feature (forceSingle) → `code_parallel`** (`b07a3a8c-…`): `route=single` → `code_parallel` →
  **delivered=true**; verified on the branch — `sub(a, b)` added, 2 tests green.

## Tests

`workers/tests/test_stages.py`: intake routing by kind/mode, the clarify gate (only with a human), the
coding_agent-output parse; the repro gate's red/green/blocked classification; the FIXED/NOT_FIXED/
CANNOT_REPRODUCE verdict including the H13 tamper guard (a fix that weakens the repro test →
NOT_FIXED); and `bug_adopt`'s fast-forward. Full suite green (1605 passed, 3 skipped); invariant audit
clean.

## Recommendation

**Phase 4 complete → proceed to Phase 5 (greenfield on-ramp, G3 `project_bootstrap`).** A raw request
now enters one front door and is classified and routed to the right engine; a bug is fixed only when
genuinely reproduced and proven green without tampering with its own test.
