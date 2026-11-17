## ADDED Requirements

### Requirement: A program defines done before it builds
`cc_program` SHALL produce an outcome contract after planning and before the first rank is built. The contract SHALL list every check with an id, a kind (`acceptance`, `client`, `scenario`, `budget`, `docs`, `integration`), the claim it proves, the command that runs it, its setup, its weight, and SHALL name the delivery threshold. The agent-authored frozen acceptance tests and the final `test_cycle` SHALL be included as checks. The contract SHALL be recorded on the program board as an `outcome_contract` fact with `revision: 1`.

#### Scenario: Contract recorded before rank one
- **WHEN** a program's plan passes the decomposition gate
- **THEN** an `outcome_contract` fact with revision 1 exists on the program board before `program_rank_build` runs
- **AND** it names a delivery threshold and at least the acceptance and integration checks

### Requirement: External checks are found by scouts, not written by the builders
When `externalChecks` is true (the default) the program SHALL fork one scout session per check kind (`client`, `scenario`, `budget`, `docs`). A scout SHALL look for a real client, conformance suite, scenario, budget check or docs check that proves the request's claims, install it under the run's worktree cache directories, run it against the untouched head, and return the command, setup, observed output and the claim it proves, or `none found` with its reasoning. A scout SHALL NOT edit the repository tree; any such edit is reverted and recorded.

#### Scenario: A client suite is found for a redis-compatible server
- **WHEN** the request is a redis-compatible server and the `client` scout runs
- **THEN** it returns a command that runs `redis-cli` or a `redis-py` test slice against the server port named in the request
- **AND** the setup installs the client under the worktree's cache directory, not in the repository tree

#### Scenario: No check exists for a kind
- **WHEN** a scout finds nothing it can run for its kind
- **THEN** it returns `none found` with its reasoning
- **AND** the contract records that kind as absent so the console shows the contract has no check of that kind

### Requirement: Every check is red-proved before it enters the contract
`contract_fold` SHALL run, or read the scout's run of, every candidate check against the untouched head. A candidate that passes SHALL be dropped with mechanism `vacuous: passed before any work`. A candidate whose setup fails SHALL be kept with state `blocked` and the exact mechanism, SHALL count as red in the score, and SHALL never be recorded as passed.

#### Scenario: A vacuous check is dropped
- **WHEN** a candidate check passes against the untouched head
- **THEN** it is not in the contract
- **AND** the fold's output names it and the mechanism

#### Scenario: A blocked check stays red
- **WHEN** a candidate's setup fails because a tool cannot be installed
- **THEN** the contract holds it with state `blocked` and the mechanism
- **AND** the score treats it as not passing

### Requirement: The contract is approved or rejected by a reviewer on a different model
The contract SHALL be reviewed by a `contract_review` session whose model differs from the define session's. The reviewer SHALL return `approve` or `reject` with findings; it SHALL NOT send the contract back for open-ended edits. On reject the define session revises and the loop repeats, bounded by `maxRescues` with tier escalation. When the bound is reached, the reviewer's last findings SHALL be applied as weight reductions, the contract proceeds, and an `amendment` fact with mechanism `review bound reached` records it.

#### Scenario: Reviewer rejects a contract
- **WHEN** the reviewer returns `reject` with findings
- **THEN** the define session runs again with those findings
- **AND** the next contract fact has the next revision number

#### Scenario: Review bound reached
- **WHEN** the review loop reaches `maxRescues` without an approval
- **THEN** the contract proceeds with the reviewer's last findings applied as weight reductions
- **AND** an `amendment` fact records mechanism `review bound reached`

### Requirement: The score is computed from the contract
The program SHALL compute `score = Σ weight(passing checks) / Σ weight(all checks)` over the current contract revision after every `contract_run`, and SHALL record it in an `evaluation` fact with the head it was measured at. The same head SHALL yield the same score for the same revision. A program is at threshold when `score >= threshold`.

#### Scenario: Reproducible score
- **WHEN** `contract_run` is executed twice at the same head and revision
- **THEN** both `evaluation` facts carry the same score

#### Scenario: Weighted share
- **WHEN** checks of weights 3, 1 and 1 pass, fail and fail
- **THEN** the score is 0.6

### Requirement: Checks run through the shared runtime, each recorded
`contract_run` SHALL execute every check command in the run's worktree through the shared execution runtime (`common/exec.py` or `check_execution.inherited_environment()`), SHALL run every check even after one fails, SHALL record one `check_result` fact per check (exit status, output tail, head, wall time), and SHALL NOT add any execution deadline.

#### Scenario: One failure does not stop the others
- **WHEN** the first check fails
- **THEN** every remaining check still runs and has its own `check_result` fact

### Requirement: Contract changes are amendments with a reviewer decision
Any change to the contract after revision 1 (drop, reweight, replace a check, mark an item satisfied, change the threshold) SHALL be an `amendment` fact proposed by the Architect, the second-opinion agent, or a human steer, SHALL be approved or rejected by the `contract_review` session (a human `amend` steer counts as that decision), and when approved SHALL produce the next `outcome_contract` revision. The score SHALL be recomputed against the new revision.

#### Scenario: Amendment approved
- **WHEN** the Architect proposes dropping a check that depends on an out-of-scope feature and the reviewer approves
- **THEN** a new contract revision exists without that check
- **AND** the next evaluation's score is computed against the new revision

#### Scenario: Amendment rejected
- **WHEN** the reviewer rejects an amendment
- **THEN** the contract revision is unchanged
- **AND** the rejection and its findings are recorded on the amendment fact
