## ADDED Requirements

### Requirement: The run page shows the definition of done and the score
The web console's run page for a `cc_program` run SHALL show a "Definition of done" panel listing each check of the current contract revision (kind, claim, weight, state red, green or blocked, output tail from its latest `check_result`), the score history per evaluation as a bar that climbs, the current ladder rung, and open needs. The TUI run detail SHALL show the score, the rung and the needs.

#### Scenario: Panel on a program run
- **WHEN** a `cc_program` run with an `outcome_contract` fact is opened
- **THEN** the panel lists every check with its state and the latest score
- **AND** a run without a contract shows no panel

#### Scenario: Blocked check visible
- **WHEN** a check is in state `blocked`
- **THEN** the panel shows it as blocked with the mechanism, not as failed or passed

### Requirement: The run page lets a human steer the Architect
The run page SHALL offer a "Steer the architect" form with the four steer shapes (`feedback`, `accept` an item, `amend` approve or reject a pending amendment, `budget`). Posting SHALL require a registered actor token as every steering write does, and the posted steer SHALL appear in the run's activity with the actor's name.

#### Scenario: Feedback posted
- **WHEN** an actor posts feedback from the form
- **THEN** a `steer` fact with `authority: human` and the actor as author exists on the program board
- **AND** the activity view shows it

### Requirement: Continue from a need
A run that ended `PROGRAM_NEEDS` SHALL show its needs and a Continue action. Continue SHALL launch the continuation run with `continueFrom` the ended run and, when a `budget` steer is pending, the raised ceiling. The run state SHALL render as "needs" with the need named, not as failed.

#### Scenario: Continue after budget
- **WHEN** a run ended with need `budget` and the actor posts a `budget` steer then clicks Continue
- **THEN** a new run starts with `continueFrom` the ended run and the raised `ceilingUsd`
- **AND** the ended run's lifecycle actions include `retryUnfinished`
