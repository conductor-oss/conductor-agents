# Program Scale & Lasting Memory — Phase 6 Result (G4 + G5)

**Status: shipped and live-proven.** A program is now planned into subsystems and built in dependency
order on a shared branch with cross-subsystem integration (`cc_program`, G4), and the harness keeps
persistent, on-branch codebase knowledge that later runs read — with stale lessons invalidated rather
than trusted (`repo_memory`, G5/H10). Proven by a real Conductor execution plus a direct decay proof on
the built artifact.

## What shipped

- **`cc_program` (G4)** (`workflows/cc_program.json` + `common/program.py` + `stages/` workers): reads
  on-branch `repo_memory` (decaying stale lessons) → a read-only `coding_agent` plans the program into
  subsystems with cross-subsystem `dependsOn` + frozen `contractPaths` (prior knowledge injected) →
  `intake_route` parses it → **`team_build`** builds the subsystems in dependency order on a shared
  program branch (its board already dispatches dependency-ready work in waves, integrates continuously,
  and gates green) → `program_outcome` emits a report naming the topological ranks (or
  `PROGRAM_PLAN_CYCLIC` for a non-DAG) → `repo_memory_write` persists the run's lessons. (Teams-of-teams
  nesting, H3, is the scaling path the board is built for; at this scale the program board is the board.)
- **`repo_memory` (G5)** (`common/repo_memory.py` + `repo_memory_read`/`repo_memory_write`): an
  on-branch `.conductor-code/repo_memory.json` of invariants + lessons, each lesson carrying
  provenance + confidence + **refs**. `decay_lessons` invalidates any lesson whose referenced files no
  longer exist (H10/H15 — a confidently-wrong lesson is dropped before it can mislead planning); the
  digest fed to the next run contains only trustworthy lessons.

## Live proof — `cc_program` PROGRAM_DELIVERED (`c13c626d-5e0b-4b19-9ae7-4eb83748b45c`)

- **Input:** an empty-but-for-frozen-tests repo (`test_program.py` red at baseline) + *"build a task
  system as three subsystems: model; store (deps model); service (deps model + store)."*
- **Trace:** `repo_memory_read` (0 prior lessons) → planner produced subsystems
  `model` (no deps), `store` (deps `model`), `service` (deps `model`, `store`) with
  `contractPaths = ['test_program.py']` → `team_build` built them in **dependency order**
  (`code_subtask model → store → service`) → `PROGRAM_DELIVERED`, 3/3, integration green → 3 lessons
  persisted.
- **`program_outcome` ranks:** `[['model'], ['store'], ['service']]` — the topological order.
- **Independently re-verified** on the built branch: `model.py`, `store.py`, `service.py` present,
  history is `model → store → service`, and the suite (frozen integration tests + per-subsystem tests)
  is **9 passed**.

### A real planner defect found and guarded (H4/H14)

The first run (`2f3ff145`) came back `PROGRAM_INCOMPLETE`, 0/3: the planner had listed the subsystems'
own implementation files (`model.py`/`store.py`/`service.py`) as frozen `contractPaths`, so every
subsystem's edit was a contract violation and `model` blocked, starving its dependents — and with
nothing integrated the final `test_cycle` reported a *vacuous* `no_tests_required` green. Fixed with a
robust invariant in `intake.plan_route`: **a contract path that is also a subsystem's file is dropped**
(you cannot freeze a file you are assigning someone to build), plus a sharper planner prompt. Re-run →
`contractPaths = ['test_program.py']` → PROGRAM_DELIVERED.

## Live proof — `repo_memory` plans from prior knowledge and decays stale lessons (G5/H10)

On the delivered program's worktree, using the real `repo_memory` workers:
- Persisted 3 lessons referencing `model.py` / `store.py` / `service.py`.
- A later **read returned all 3 live** — the knowledge a second run plans with.
- The code then moved on (`service.py` removed); the next read **invalidated exactly that lesson**
  (`live = 2, invalidated = 1`), leaving `model` and `store` live. A stale lesson is demonstrably
  invalidated rather than trusted.

(`cc_program` now writes memory to the built branch's worktree, where the referenced files live, so
memory and code travel together.)

## Tests

`workers/tests/test_program_memory.py`: topological ranks + cycle detection, the program report
verdicts (DELIVERED / INCOMPLETE / PLAN_CYCLIC), the decayable-lesson emission, and the
write→read→decay roundtrip (a removed ref invalidates its lesson). `test_stages.py` adds the
contract/file-overlap guard. Full suite green; invariant audit clean (39 workflows).

## Recommendation

**Phase 6 complete — the roadmap's six build phases are done.** A greenfield idea or a raw request now
flows through one front door to the right engine; large coupled work is built by a collaborating team
on shared code with monitors and human steer-by-exception; a program is built in dependency order; and
the repo accumulates knowledge that later runs trust only while it stays true.
