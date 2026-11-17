## ADDED Requirements

### Requirement: The Architect is a persistent role on the program board
The Architect's state SHALL live on the program board as facts (`outcome_contract`, `check_result`, `evaluation`, `amendment`, `override`, `steer`, `need`), never only in an agent session. Every Architect session's prompt SHALL be built from those facts, so the role survives worker reloads, run continuations and Retry.

#### Scenario: Continuation finds the Architect's state
- **WHEN** a program run is continued from a previous run of the same lineage
- **THEN** the define phase finds the existing `outcome_contract` and skips definition
- **AND** the evaluation history and ladder rung from the previous run are read, not restarted

### Requirement: The Architect plans and defines done in one session
The existing `program_plan` session SHALL act as the Architect's define session: it keeps its subsystem plan, schema and decomposition gate, and additionally returns candidate checks (claim, kind, weight), the delivery threshold and notes for the scouts. The decomposition gate's placeholder rules SHALL apply to check claims as they do to items.

#### Scenario: Plan and contract draft from one session
- **WHEN** the define session returns
- **THEN** its structured output holds both the subsystems and the contract draft
- **AND** a draft whose check claims are placeholders is rejected by the gate like a placeholder item

### Requirement: The Architect evaluates every integrated rank
After `program_apply_rank` folds a rank, the program SHALL run `contract_run` and an `architect_evaluate` session that reads the integrated branch, the design documents, the contract and the check results, and returns `items` (id, description, files, acceptance criteria, subsystem) for gaps it finds and `directives` for subsystems still to build. `architect_fold` SHALL post the items on the program board for the remaining subsystems, post the directives with `authority: agent`, and record an `evaluation` fact with the score, the rank and the items filed.

#### Scenario: Cross-team gap becomes an item
- **WHEN** the Architect finds that the zsets store exposes an API the dispatcher team does not use
- **THEN** an item with files and acceptance criteria is posted on the program board under the subsystem that owns the dispatcher
- **AND** the next `program_rank_build` forks it as that subsystem's work

#### Scenario: Placeholder finding rejected
- **WHEN** the Architect returns an item with no files or no acceptance criteria
- **THEN** `architect_fold` rejects it with the placeholder mechanism and it is not posted

### Requirement: The Architect signs off against the contract
After the final `test_cycle`, the program SHALL run `contract_run` and an evaluation; the program is delivered only when the score is at or above the threshold, or an `override` fact exists for the current contract revision. `program_outcome` SHALL report the contract, each check's result and the score history.

#### Scenario: Delivered at threshold
- **WHEN** the final evaluation's score is at or above the threshold
- **THEN** `program_outcome` reports `PROGRAM_DELIVERED` with each check's result

#### Scenario: Not delivered under threshold
- **WHEN** the final evaluation's score is under the threshold and no override exists
- **THEN** the program enters the convergence ladder rather than reporting a verdict

### Requirement: The Architect never implements
The define, evaluate and second-opinion sessions SHALL be read-only agent sessions. Any file edit they attempt is reverted and recorded with mechanism `architect wrote to the repository`.

#### Scenario: Architect edit reverted
- **WHEN** an evaluate session edits a source file
- **THEN** the edit is reverted before the fold
- **AND** the fold records the mechanism on the evaluation fact

### Requirement: Teams still INCOMPLETE after their last attempt go to the ladder, not to BLOCKED
A subsystem whose team is still INCOMPLETE after `maxTeamAttempts` SHALL NOT be set `BLOCKED`. Its unfinished items SHALL be handed to the Architect's evaluation, which files them for the convergence ladder's `change_approach` rung.

#### Scenario: Last attempt exhausted
- **WHEN** a team ends INCOMPLETE on its last allowed attempt
- **THEN** the subsystem is not in state `BLOCKED`
- **AND** the ladder's next rung for its items is `change_approach`
