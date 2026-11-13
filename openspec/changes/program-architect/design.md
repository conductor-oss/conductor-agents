## Context

`cc_program` today: `model_profile_resolve` → `repo_memory_read` → plan (a read-only `coding_agent` plus the `intake_route` decomposition gate, re-planned up to `maxReplan`) → `workspace_prepare` → `program_rank_init` (program board, frozen contract paths, file-disjoint ranks) → `rank_loop` (`program_rank_build` forks one `team_build` per subsystem, `program_apply_rank` folds verdicts, re-runs an INCOMPLETE team up to `maxTeamAttempts`, re-slices from delivered design docs) → `test_cycle` → `program_outcome` → `repo_memory_write` → retrospective. The program's verdict is "every subsystem item DONE and the final test cycle green". Nothing checks the integrated system against the request itself, and the only checks that exist were written by the same agents who built against them.

Constraints that shape this design (from `AGENTS.md`): one workflow version, zero JQ, no WAIT or human gate in a swarm workflow, no timeouts, child processes through `common/exec.py`, `docs/workflow-inputs.md` in sync, Simplicity Policy (every new task justified by a requirement the existing structure cannot express), Gap-Closure Policy (no caveats, prove it live). The first principle the user set for this change: the swarm's goal is to find a way to make the work succeed with the inputs it has, never to find a reason to stop.

## Goals / Non-Goals

**Goals:**
- A definition of done the agents cannot fool: external checks, red-proved, reviewed by a second model, weighted, scored.
- One role that owns the whole-system view across ranks and files work items that re-enter the normal team cycle.
- Guaranteed convergence: every round moves the score or the approach changes; the program has one terminal state, delivered; everything else is a rung or a named need that a continuation resumes.
- Humans steer asynchronously; nothing waits on them.
- Visible in the console: the score climbing, the rung, the needs.

**Non-Goals:**
- A per-domain conformance library in repo memory (follows once the contract is proven).
- Changing the `team_build` cycle. Teams stay as they are; the Architect speaks to them through board items and directives they already read.
- In-place resumption of a Conductor execution that has ended. Resumption is a continuation run that re-reads the Architect's state (see D7).

## Decisions

**D1. The Architect is a role on the board, not an agent process.** Its state is a set of fact kinds on the program board (`outcome_contract`, `check_result`, `evaluation`, `amendment`, `override`, `steer`, `need`), and each of its duties is one agent session whose prompt is built from that state. Alternative: a long-lived agent session carried through the run. Rejected: sessions die with worker reloads and cannot be continued across runs; the board survives both and is already what Retry-unfinished rebuilds from.

**D2. Definition of done is a small team, not one agent, and not the full team cycle.** Shape: `FORK_JOIN_DYNAMIC` of *scouts* (one `coding_agent` write session per check kind: client/conformance suite, scenario, budget, docs), then a deterministic `contract_fold`, then a `contract_review` agent, inside a `DO_WHILE` bounded by `maxRescues`. The scouts are needed because finding, installing and running `redis-py` is work in a worktree with tools, which a planning session cannot do; the fold is deterministic so the red-proof rule cannot be argued with; the reviewer is a different model so author and judge are not one mind. The full team cycle (standup, ownership, rebase, peer review) has no job here: there is no code to integrate. Scouts install into the run's worktree under `.cc-cache` and scratch dirs, never into the repository tree; the fold reverts anything else, the same rule `deniedWrites` applies to Codex.

**D3. The planner session becomes the Architect's define session.** `program_plan` keeps its prompt, schema and decomposition gate, and gains the contract draft (candidate checks with claim, weight and delivery threshold) and the `architect_notes` the scouts need. One session rather than two because the plan and the definition of done are the same judgment about the request; a separate session would re-read everything to say the same thing. The scouts then turn candidates into runnable, red-proved checks.

**D4. Red-proof is a hard rule, applied by code.** A check that passes against the untouched head is dropped by `contract_fold` with the reason recorded; one whose setup fails is kept as `blocked` with the exact mechanism (tool not installable, port busy), never as passed, and counts as red in the score until it runs. This is the existing "blocked discovery is never a pass" rule applied to checks.

**D5. The score is computed, never felt.** `architect.score(contract, results) = Σ weight(passing checks) / Σ weight(all checks)`, over the contract's current revision. Weights and the delivery threshold are set when the contract is defined and change only through an amendment. The Architect's judgment goes into weights and into the work items it files, not into the number. The same integrated head scores the same on every run, which is what makes the progress rule (D6) measurable and the console bar honest.

**D6. The ladder is a decision function, not workflow branches.** `architect.next_rung(history, score, threshold, max_repair_rounds, steer, budget)` returns one of `rework`, `change_approach`, `second_opinion`, `needs`, or `deliver`. Rules: a round that does not raise the score counts against the current rung; after `maxRepairRounds` without improvement the rung advances; `change_approach` requires a plan whose items differ from the last plan's (the fold rejects a repeat); `second_opinion` runs once per contract revision; `needs` only when the budget share is spent or a check is `blocked` on something outside the swarm's reach, and it names the need. The workflow has one `DO_WHILE convergence_loop` whose body is `contract_run` → `architect_evaluate` (agent) → `architect_fold` → a `SWITCH` on the rung that forks a repair `team_build`, or runs the second-opinion agent, or exits. One loop with a switch, rather than one loop per rung, so the whole ladder is readable in one place.

**D7. Resumption is a continuation run, keyed to the Architect's state.** A Conductor execution cannot be parked without a WAIT, and WAIT is forbidden in swarm workflows. So a program that reaches `needs` ends its run with `verdict: PROGRAM_NEEDS`, `needs: [...]`, `programDelivered: false`, and the console's Continue (the existing Retry-unfinished continuation, `continueFrom`) launches the next run with the same `correlationId` lineage; `architect_define` finds an existing `outcome_contract` for the lineage and skips definition, and the convergence loop starts at the recorded rung with the recorded history. The budget halt in `harness-budget-gate` is unchanged; only what follows it changes.

**D8. Human steer is a fact, read at every evaluation.** `/api/steer` gains target `architect` with four shapes: `feedback` (text), `accept` (item id, reason), `amend` (approve or reject a pending amendment by id), `budget` (raise `ceilingUsd` for the continuation). Each is a `steer` fact with `authority: human`, folded by `architect_fold` on its next pass and shown in the Architect's prompt as unseen input. An `accept` marks the item satisfied in the contract as a human-authored amendment, recorded like any other. Nothing waits on steer; the loop runs the same with or without it.

**D9. Second opinion can override, with a record.** The `second_opinion` agent (a model different from the Architect's, read-only) rules on the red items: the work meets the requirement, the contract is wrong, or the work is short. "Meets" writes an `override` fact with the reasoning and the program delivers; "contract is wrong" becomes a pending `amendment` for the reviewer; "short" sends the ladder back to `change_approach` with its findings as items. One override per contract revision, so it cannot be used twice to wave the same gap through.

**D10. Per-rank evaluation files items, not comments.** After each rank the Architect reads the integrated branch, the design docs and the contract and returns `items` (id, description, files, acceptance criteria, which subsystem they belong to). `architect_fold` posts them on the program board as items of the remaining subsystems (so the next `program_rank_build` builds them) or, when every subsystem is built, as the repair team's work. Findings with no files or no criteria are rejected by the fold with the same placeholder rule the decomposition gate uses. The Architect also returns `directives` for subsystems still to build, posted with `authority: agent`, which standups already read.

**D11. Checks run through `verification`/`exec`, each recorded.** `contract_run` executes every check command in the run's worktree with `check_execution.inherited_environment()`, records an `check_result` fact per check (exit, output tail, head), runs every check even after one fails, and never runs a check whose command edits the repository tree. No deadlines; a check runs until it completes.

**D12. Removed verdicts.** `PROGRAM_INCOMPLETE` goes; `program_outcome` returns `PROGRAM_DELIVERED` or `PROGRAM_NEEDS`. The `BLOCKED` subsystem state from budget exhaustion in `program_apply_rank` becomes a `need`; a team still INCOMPLETE after `maxTeamAttempts` no longer blocks its subsystem, its red items go to the ladder as `change_approach`. `test_cycle` after the rank loop stays as the integration check and is listed in the contract as a check with its own weight.

## Risks / Trade-offs

- [Scouts cannot find or install an external check for the domain] → The fold records `none found` per kind with the scout's reasoning; the contract is then the frozen tests plus `test_cycle`, and the reviewer sees exactly that. A contract with no external check is allowed but shown as such in the console, so the weakness is visible rather than hidden.
- [A wrong check in the contract makes the threshold unreachable] → The amendment procedure (Architect or second opinion proposes, reviewer approves or rejects, human can approve via steer) and the progress rule guarantee the ladder reaches second opinion rather than looping on rework. The redis live proof includes one such check on purpose.
- [The Architect and the reviewer deadlock on the contract] → Bounded by `maxRescues` with tier escalation, as frozen-test review is; after the bound the reviewer's last findings are applied by the fold as weight reductions and the contract proceeds, recorded as an amendment with mechanism `review bound reached`.
- [Score plateaus just under threshold at the top tier with no human present] → The ladder reaches `second_opinion`, then `needs` naming "owner decision on items X, Y with red output attached"; the run ends `PROGRAM_NEEDS`, not delivered, and Continue resumes it. This is the one place the program asks, and it asks a precise question.
- [Check commands are expensive (a full `redis-py` suite)] → Scouts are asked for the smallest slice that proves the claim (a test subset selected by command names in scope); the fold records the command's wall time per run for the console. No deadline is added.
- [Breaking the `PROGRAM_INCOMPLETE` verdict] → `programDelivered` is unchanged and is what the console and TUI branch on; the verdict string appears in reports only. The console gains the `PROGRAM_NEEDS` rendering in the same change.
- [More agent calls per program] → Four cheap scouts, two stronger sessions at definition, one evaluation per rank, and the ladder only when under threshold. On redis that is a few dollars against a program that costs tens.

## Migration Plan

1. Register `cc_program` v1 in place (`make register`); running executions keep their version by the register script's rule.
2. Workers restart by `source_watch` on `workers/**`; orphan reclaim handles in-flight deterministic tasks.
3. Console restart (reads `console.html` once at startup).
4. Live proof on the redis branch as a continuation; then `runtime_health`.
Rollback: revert the commit; the new fact kinds on existing boards are ignored by the previous code.

## Open Questions

- Which `redis-py` test subset the scouts should prefer is a prompt-tuning question answered by the live proof, not a design decision.
