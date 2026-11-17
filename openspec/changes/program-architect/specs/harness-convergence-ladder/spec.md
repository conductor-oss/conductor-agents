## ADDED Requirements

### Requirement: The only terminal verdict is delivered
`cc_program` SHALL report `verdict` as `PROGRAM_DELIVERED` or `PROGRAM_NEEDS`. `PROGRAM_INCOMPLETE` SHALL NOT be produced. `PROGRAM_NEEDS` SHALL name each need precisely (`budget` with the amount spent and the ceiling, `owner decision` with the red items and their output, `credential` with what for) and SHALL leave the Architect's state on the board for a continuation.

#### Scenario: No graded failure
- **WHEN** a program ends its run without reaching the threshold
- **THEN** its verdict is `PROGRAM_NEEDS` with at least one named need
- **AND** `programDelivered` is false

### Requirement: The ladder is taken in order, automatically
When the final evaluation is under the threshold, the program SHALL enter a convergence loop whose rung is decided by `architect.next_rung` from the evaluation history: `rework` (a repair `team_build` on the red items, one tier stronger each round), then `change_approach` (the Architect produces a plan for the red items whose items differ from the last plan's; a repeat is rejected by the fold), then `second_opinion`, then `needs`. No rung SHALL wait on a human.

#### Scenario: Rework first
- **WHEN** the first final evaluation is under the threshold
- **THEN** the next rung is `rework` and a repair team is forked with the red items and their check output

#### Scenario: Repeat plan rejected
- **WHEN** the `change_approach` session returns items identical to the previous plan's
- **THEN** the fold rejects the plan with mechanism `same approach as before` and the session runs again at the next tier

### Requirement: Every round must raise the score or the rung advances
A round that does not raise the score SHALL count against the current rung. After `maxRepairRounds` (default 2) rounds on a rung without a score increase, the ladder SHALL advance to the next rung. A round that raises the score SHALL reset the rung's count.

#### Scenario: Progress keeps the rung
- **WHEN** a rework round raises the score from 0.6 to 0.8
- **THEN** the next rung is still `rework`

#### Scenario: No progress advances the rung
- **WHEN** two rework rounds leave the score at 0.8
- **THEN** the next rung is `change_approach`

### Requirement: Second opinion can override, amend or send back, once per revision
The `second_opinion` session SHALL run on a model different from the Architect's and SHALL return one of: `meets` (the work satisfies the requirement; an `override` fact is recorded with the reasoning and the program delivers), `contract wrong` (a pending `amendment` for the reviewer), or `short` (its findings become items and the ladder returns to `change_approach`). It SHALL run at most once per contract revision.

#### Scenario: Override delivers
- **WHEN** the second opinion returns `meets`
- **THEN** an `override` fact exists for the current revision
- **AND** the program reports `PROGRAM_DELIVERED` naming the override

#### Scenario: One override per revision
- **WHEN** an `override` fact already exists for the current revision and the score is again under threshold
- **THEN** `second_opinion` is not run again for that revision

### Requirement: Human steer is asynchronous input the Architect reads at every evaluation
`/api/steer` SHALL accept target `architect` with shapes `feedback` (text), `accept` (item id, reason), `amend` (amendment id, approve or reject) and `budget` (new `ceilingUsd`). Each SHALL be recorded as a `steer` fact with `authority: human`. `architect_fold` SHALL read unseen steer facts on every pass: `feedback` goes into the next Architect prompt, `accept` becomes an approved human amendment marking the item satisfied, `amend` decides the pending amendment, `budget` is applied by the next continuation. Nothing in the program SHALL wait on a steer; `test_swarm_workflows_never_wait_for_a_human` SHALL stay green.

#### Scenario: Accept marks an item satisfied
- **WHEN** a human posts `accept` for a red item
- **THEN** the next contract revision marks that check satisfied by human amendment
- **AND** the next score counts it as passing

#### Scenario: Steer never blocks
- **WHEN** no steer is posted for the whole run
- **THEN** the ladder proceeds through every rung without waiting

### Requirement: Needs resume by continuation at the recorded rung
When the ladder reaches `needs`, the program SHALL record a `need` fact and end its run with `PROGRAM_NEEDS`. The console's Continue action SHALL launch a continuation run (`continueFrom` the ended run) that reads the lineage's Architect state and resumes the convergence loop at the recorded rung and history, not from definition. A `budget` steer raises `ceilingUsd` for that continuation.

#### Scenario: Budget spent
- **WHEN** the program's spend reaches its ceiling with the score under threshold
- **THEN** the run ends `PROGRAM_NEEDS` with need `budget` naming spent and ceiling
- **AND** a continuation launched after a `budget` steer resumes at the recorded rung with the raised ceiling

#### Scenario: Continuation does not redefine done
- **WHEN** a continuation run starts for a lineage that holds an `outcome_contract`
- **THEN** no scout or review session runs
- **AND** the first task after workspace preparation is `contract_run`
