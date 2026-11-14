## Why

The swarm now rarely stops, but when it finishes, the proof that it built the right thing is only as strong as the acceptance tests its own agents wrote up front. On the redis program (runs `688f426b`, `373cac75`) the `resp-reader` test was wrong in a way no one could correct, two acceptance tests did not check what they claimed, and "RESP compliant" was never checked against anything real; several defects (the zsets store API, dispatcher ownership) fell between teams because nobody owned the whole-system view. And every place the program could not approve its own output, it ended with a graded failure (`PROGRAM_INCOMPLETE`, a `BLOCKED` subsystem), which is the system excusing itself rather than finishing the job.

## What Changes

- **An Architect role in `cc_program`.** One persistent role, with its state on the program board, that turns the request into a weighted **definition of done**, plans the ranks, evaluates each integrated rank against that definition, files concrete work items for the gaps, and signs the program off. The Architect never implements code.
- **Outcome contract with external checks.** The definition of done is a contract of checks: the agent-authored frozen acceptance tests (as today) plus checks the agents did not write, found and installed by scout agents: a real client or conformance suite (`redis-cli`, the `redis-py` suite against the server), a scenario script, a size/performance budget, a docs check. Each check is run once before any work and must be red, or it is rejected as vacuous. The contract is approved or rejected by a reviewer agent on a different model, under the same approve-or-reject rule frozen tests have.
- **A computed 0.0 to 1.0 score.** Each check carries a weight set at definition time; the score is the weighted share that passes at the integrated head. It is reproducible: the same head scores the same. The contract names the delivery threshold. The score is what the console shows climbing, rank by rank.
- **A non-terminal convergence ladder.** When the score is under the threshold the program takes, in order and automatically: rework (repair team, stronger tier each round), change of approach (a different plan for the red items, never the same plan with a bigger model), a second-opinion agent that may override the verdict or amend the contract by procedure, and asynchronous human steer the Architect reads at every evaluation. Every round must raise the score or the program moves down the ladder. Contract amendments (drop, reweight, replace a check) go through the reviewer and are recorded as contract revisions.
- **The only terminal state is `PROGRAM_DELIVERED`.** `PROGRAM_INCOMPLETE` and the budget `BLOCKED` ending are removed. A program whose budget share is spent, or that waits on something outside the swarm's reach, ends its run with `PROGRAM_NEEDS` naming exactly what it needs (budget, a credential, an owner decision), keeps its Architect state, and the console's Continue action resumes it at the ladder rung it was on, not from scratch. **BREAKING**: `cc_program` output `verdict` no longer takes the value `PROGRAM_INCOMPLETE`; `programDelivered` is unchanged.
- **Console.** The run page gains a "Definition of done" panel (checks, weights, red or green with output tail, score history per rank, current ladder rung, open needs) and a "Steer the architect" form (feedback, accept an item, approve an amendment, add budget and continue). The TUI run detail shows the same score and rung.
- **New `cc_program` inputs** `externalChecks` (default `true`) and `maxRepairRounds` (rounds per rung before the ladder moves on, default `2`), documented in `docs/workflow-inputs.md`.

## Capabilities

### New Capabilities
- `harness-outcome-contract`: how the definition of done is defined, scouted, red-proved, reviewed, scored and amended.
- `harness-program-architect`: the Architect role: planning, per-rank evaluation of the integrated branch, work items for gaps, sign-off, persistence across ranks and continuations, never implementing.
- `harness-convergence-ladder`: the escalation ladder, the progress rule, second opinion and override, asynchronous human steer, `PROGRAM_NEEDS` and continuation; the only terminal state is delivered.
- `harness-architect-console`: the console and TUI surface for the contract, score, ladder and steer.

### Modified Capabilities
- none. `harness-budget-gate`'s clean halt at the ceiling is unchanged; what follows the halt (a need and a continuation instead of a final verdict) is specified under `harness-convergence-ladder`. `harness-shared-code-convergence`'s controlled contract-change event is reused as is for amendments.

## Impact

- `coding-harness/workers/workflows/cc_program.json`: definition phase after planning (scout fork, contract fold, contract review loop), an evaluation after every rank and after the final `test_cycle`, the convergence loop, `PROGRAM_NEEDS` outcome. One version, no JQ, no WAIT, no timeouts.
- `coding-harness/workers/stages/tasks.py` and a new `coding-harness/workers/common/architect.py` (pure logic: contract shape, weights, score, ladder decision, amendment and steer folding) with thin `@worker_task` wrappers; new prompts `program_architect_define.md`, `check_scout.md`, `contract_review.md`, `architect_evaluate.md`, `second_opinion.md`.
- `coding-harness/workers/common/team_board.py` and the blackboard: new fact kinds `outcome_contract`, `check_result`, `evaluation`, `amendment`, `override`, `steer`, `need`.
- `coding-harness/tui/console.py`, `console.html`, `screens/run_detail.py`: the panel, the steer form, Continue from a need.
- `coding-harness/docs/workflow-inputs.md`, `coding-harness/tui/README.md`, `coding-harness/README.md`.
- Tests: `workers/tests/test_architect.py` (new), `test_program_teams.py`, `test_workflows.py`, `test_team_cycle.py::test_swarm_workflows_never_wait_for_a_human` (must stay green), `tui/tests/test_console.py`.
- Live proof: the redis program relaunched as a continuation of branch `swarm/redis-compatible-server-go-set-373cac75` with external checks on, one check deliberately expected to be wrong so the ladder's amendment path is exercised, ending `PROGRAM_DELIVERED` with the `redis-py` slice green.
