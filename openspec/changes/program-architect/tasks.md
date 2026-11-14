# Tasks

Honor `AGENTS.md` throughout: one workflow version (v1), zero JQ, no WAIT or human gate, no execution deadlines, child processes through `common/exec.py` or `check_execution.inherited_environment()`, every new task justified by a requirement, `docs/workflow-inputs.md` in sync, precise failure mechanisms. Groups are file-disjoint; group 1 is the pure logic everything else calls, groups 2 to 6 build on it, group 7 is the live proof.

## 1. Pure logic: contract, score, ladder, steer

Files: coding-harness/workers/common/architect.py, coding-harness/workers/tests/test_architect.py
Test: coding-harness/workers/.venv/bin/python -m pytest -q coding-harness/workers/tests/test_architect.py

- [x] 1.1 Define the contract shape: `Check` (id, kind, claim, command, setup, weight, state, mechanism), `Contract` (revision, threshold, checks, absentKinds), the `DRAFT_SCHEMA` the define session returns and the `SCOUT_SCHEMA` a scout returns.
- [x] 1.2 `fold_contract(draft, scout_results, acceptance_tests, integration_check)`: builds revision 1; drops candidates that passed red-proof with mechanism `vacuous: passed before any work`; keeps setup failures as `blocked` with the mechanism; records absent kinds.
- [x] 1.3 `score(contract, results)`: weighted share passing; `blocked` and missing results count as red; reproducible for the same head and revision.
- [x] 1.4 `apply_amendment(contract, amendment, decision)`: drop, reweight, replace, satisfy (human accept), threshold; produces the next revision only on approve; `review bound reached` applies findings as weight reductions.
- [x] 1.5 `next_rung(history, score, threshold, max_repair_rounds, overrides, needs)`: `deliver`, `rework`, `change_approach`, `second_opinion`, `needs`; progress resets the rung count; no progress for `max_repair_rounds` advances; `second_opinion` at most once per revision; `needs` only on budget spent or a blocked check outside the swarm's reach.
- [x] 1.6 `same_approach(previous_items, items)` for the repeat-plan rejection, and `fold_findings(items)` applying the placeholder rule (files and acceptance criteria required).
- [x] 1.7 `fold_steer(facts, seen)`: feedback, accept, amend, budget; returns the prompt input, the amendments to apply and the new ceiling.
- [x] 1.8 `program_report` extension: `PROGRAM_DELIVERED` or `PROGRAM_NEEDS` with needs, checks, score history; no `PROGRAM_INCOMPLETE`.
- [x] 1.9 Tests for every function above, including the weighted-share example (3/1/1 → 0.6), vacuous drop, blocked stays red, progress keeps rung, two no-progress rounds advance, one override per revision, repeat plan rejected, steer accept marks satisfied.

## 2. Worker tasks and prompts

Files: coding-harness/workers/stages/tasks.py, coding-harness/workers/common/program.py, coding-harness/workers/defaults/prompts/program_plan.md, coding-harness/workers/defaults/prompts/check_scout.md, coding-harness/workers/defaults/prompts/contract_review.md, coding-harness/workers/defaults/prompts/architect_evaluate.md, coding-harness/workers/defaults/prompts/second_opinion.md, coding-harness/workers/tests/test_program_teams.py, coding-harness/workers/tests/test_stages.py
Test: coding-harness/workers/.venv/bin/python -m pytest -q coding-harness/workers/tests/test_program_teams.py coding-harness/workers/tests/test_stages.py

- [x] 2.1 Extend `program_plan`'s prompt and schema with the contract draft (candidate checks with claim, kind, weight; threshold; scout notes); extend the decomposition gate's placeholder check to check claims.
- [x] 2.2 `check_scout_build` task: one dynamic fork input per kind (`client`, `scenario`, `budget`, `docs`) from the draft, each a write-session `coding_agent` in the run's worktree with `allowedWriteRoots` limited to the cache directories; `check_scout.md` prompt (find, install under `.cc-cache`, run against the untouched head, return command/setup/output/claim or `none found`).
- [x] 2.3 `contract_fold` task: reverts scout edits outside the cache roots (recorded), calls `architect.fold_contract`, posts `outcome_contract` revision 1 (or the next revision on a review round), records planner and scout spend on the program budget; skips entirely when the lineage already holds a contract (continuation).
- [x] 2.4 `contract_review` session: `contract_review.md` prompt, approve or reject with findings, model tier chosen so it differs from the define session's; `contract_review_fold` applies the decision, bounds the loop by `maxRescues`, records `review bound reached`.
- [x] 2.5 `contract_run` task: runs every check command through `exec`/`inherited_environment()` in the worktree, every check even after a failure, one `check_result` fact each (exit, output tail, head, wall time); refuses a check whose command edits the repository tree; no deadline.
- [x] 2.6 `architect_evaluate` session: read-only `coding_agent` with `architect_evaluate.md` (integrated branch, design docs, contract, check results, unseen steer feedback → items, directives, amendments); `architect_fold` task: reverts and records any edit, posts items on remaining subsystems or as repair work, posts directives `authority: agent`, applies `fold_findings`, folds steer, records the `evaluation` fact with score and rung via `next_rung`.
- [x] 2.7 `second_opinion` session with `second_opinion.md` (meets / contract wrong / short) on a different model; its fold writes `override`, a pending `amendment`, or items for `change_approach`.
- [x] 2.8 `program_apply_rank`: a team INCOMPLETE after its last attempt hands its unfinished items to the evaluation instead of `BLOCKED`; budget exhaustion records a `need` instead of `BLOCKED`. `program_outcome` uses the extended report and includes `needs`, checks and score history.
- [x] 2.9 Repair team forking for `rework` and `change_approach` reuses `program_rank_build`'s child input (`team_build`, `forceTeam`, budget share, `baseModelTier` one up per round) with the red items and their check output as the subtasks.
- [x] 2.10 Tests: scout fork inputs, fold on continuation skip, review loop bound, contract_run records every check and refuses tree-editing commands, evaluate fold posts items and rejects placeholders, second-opinion folds, apply_rank hands last-attempt items to the ladder, outcome reports needs.

## 3. Workflow definition and registration invariants

Files: coding-harness/workers/workflows/cc_program.json, coding-harness/workers/workflows/taskdefs/architect.json, coding-harness/workers/tests/test_workflows.py, coding-harness/scripts/audit_harness_invariants.py
Test: coding-harness/workers/.venv/bin/python -m pytest -q coding-harness/workers/tests/test_workflows.py coding-harness/workers/tests/test_team_cycle.py -k "never_wait or program" && coding-harness/.venv-ci/bin/python coding-harness/scripts/audit_harness_invariants.py

- [x] 3.1 Add the definition phase after `rank_init`: `check_scout_build` → `FORK_JOIN_DYNAMIC scouts` → `JOIN` → `contract_fold` → `contract_review` → `contract_review_fold` inside a `DO_WHILE contract_loop` bounded by `maxRescues`; a `SWITCH` skips the phase when `contract_fold` reports an existing contract (continuation).
- [x] 3.2 Add the per-rank evaluation inside `rank_loop` after `apply_rank`: `contract_run` → `architect_evaluate` → `architect_fold` (the breakdown gate stays, after the fold).
- [x] 3.3 Replace the post-`test_cycle` tail with the convergence loop: `DO_WHILE convergence_loop` { `contract_run` → `architect_evaluate` → `architect_fold` → `SWITCH rung` { `rework`/`change_approach`: fork repair `team_build`; `second_opinion`: session + fold; `deliver`/`needs`: exit } } then `program_outcome`, memory write, retrospective. Inputs `externalChecks` (default true) and `maxRepairRounds` (default 2) wired through `inputParameters`.
- [x] 3.4 Task definitions for the new SIMPLE tasks (no timeouts, retry policy as siblings); `make register` leaves exactly version 1 registered.
- [x] 3.5 Tests: workflow parses, starts with `model_profile_resolve`, holds no WAIT or `humanAvailable`, zero JQ, every `test_cycle` call forwards the two test flags, the new inputs are present; audit stays clean.

## 4. Board and steer plumbing

Files: coding-harness/workers/common/team_board.py, coding-harness/workers/common/feedback.py, coding-harness/workers/tests/test_team_board.py
Test: coding-harness/workers/.venv/bin/python -m pytest -q coding-harness/workers/tests/test_team_board.py

- [x] 4.1 Register the new fact kinds (`outcome_contract`, `check_result`, `evaluation`, `amendment`, `override`, `steer`, `need`) with readers (`latest_contract`, `evaluations`, `pending_amendments`, `unseen_steer`, `open_needs`) alongside the existing board readers.
- [x] 4.2 `board_rollup` includes the latest score, rung and open needs so the program Lead, the console and the retrospective see them without re-reading facts.
- [x] 4.3 `apply_steer` accepts target `architect` with shapes `feedback`, `accept`, `amend`, `budget`, authenticated as every steering write, recorded with `authority: human` and the actor as author.
- [x] 4.4 Tests for each reader and steer shape, and that `retro_evidence` picks up overrides, amendments and needs.

## 5. Console and TUI

Files: coding-harness/tui/console.py, coding-harness/tui/console.html, coding-harness/tui/screens/run_detail.py, coding-harness/tui/tests/test_console.py, coding-harness/tui/tests/test_run_detail.py
Test: coding-harness/tui/.venv/bin/python -m pytest -q coding-harness/tui/tests

- [x] 5.1 `/api/run` and `/api/state` carry the contract, check results, evaluations, rung, pending amendments and needs for `cc_program` runs.
- [x] 5.2 "Definition of done" panel: check list (kind, claim, weight, red/green/blocked with mechanism, output tail), score history bar, rung, needs; hidden when no contract.
- [x] 5.3 "Steer the architect" form posting the four shapes to `/api/steer` with the actor token; steer shown in activity with the actor's name.
- [x] 5.4 Run state renders `PROGRAM_NEEDS` as "needs" with the need named, never as failed; `lifecycle_actions` returns `retryUnfinished` for it; Continue passes a pending `budget` steer's ceiling into the continuation launch.
- [x] 5.5 TUI run detail shows score, rung and needs; Retry on a needs run routes to the continuation.
- [x] 5.6 Tests: panel data in `/api/run`, steer shapes, needs rendering and continuation with the raised ceiling, no panel without a contract.

## 6. Documentation

Files: coding-harness/docs/workflow-inputs.md, coding-harness/tui/README.md, coding-harness/README.md, coding-harness/docs/SDLC_ROADMAP.md
Test: coding-harness/workers/.venv/bin/python -m pytest -q coding-harness/workers/tests/test_workflow_inputs_doc.py

- [x] 6.1 `docs/workflow-inputs.md`: `cc_program` section rewritten for the Architect (definition phase, scouts, red-proof, review, score, per-rank evaluation, ladder, `PROGRAM_NEEDS` and continuation), new inputs `externalChecks` and `maxRepairRounds`, `PROGRAM_INCOMPLETE` removed.
- [x] 6.2 `tui/README.md`: the panel, the steer form, Continue from a need. `README.md` and `SDLC_ROADMAP.md`: the first principle (the swarm finds a way; the only terminal state is delivered) and the Architect in the architecture summary.

## 7. Live proof

Files: coding-harness/docs/proofs/program_architect_redis.md
Test: coding-harness/workers/.venv/bin/python -m pytest -q coding-harness/workers/tests && coding-harness/tui/.venv/bin/python -m pytest -q coding-harness/tui/tests && coding-harness/.venv-ci/bin/python coding-harness/scripts/audit_harness_invariants.py

- [ ] 7.1 `make register`; worker generations reload; `runtime_health` reports `executionHealthy` and `verificationHealthy` true; console restarted.
- [ ] 7.2 Launch the redis program as a continuation of branch `swarm/redis-compatible-server-go-set-373cac75` with `externalChecks` on; record the contract the define phase produces (expect `redis-cli` and a `redis-py` slice against port 2345) and that the scouts' candidates were red-proved.
- [ ] 7.3 Include one check expected to be wrong (a `redis-py` test on a command outside the request) and record the ladder handling it: rework, then amendment through the reviewer, score recomputed.
- [ ] 7.4 Drive to `PROGRAM_DELIVERED` with the `redis-py` slice green; record the score history, the rungs taken, spend, and every real RESP gap the checks found that the frozen tests had missed.
- [ ] 7.5 Exercise `PROGRAM_NEEDS` once with a deliberately small `ceilingUsd`: the run ends naming `budget`, a `budget` steer plus Continue resumes at the recorded rung without re-running definition.
- [ ] 7.6 Write the proof document with run ids, heads, the contract, the check outputs and the console screenshots; full test suites and the audit green.
