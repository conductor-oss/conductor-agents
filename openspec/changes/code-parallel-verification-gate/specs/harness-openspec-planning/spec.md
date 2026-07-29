## MODIFIED Requirements

### Requirement: Human-or-AI-judge review loop, retargeted from design_docs.json
`openspec_plan` SHALL reuse `design_docs.json`'s existing human-or-AI-judge `DO_WHILE` review loop unchanged in mechanism, applied to the generated OpenSpec artifacts instead of `docs/design/*.md`. When the human review branch runs, the review draft SHALL include `proposal.md`'s verbatim content, not only a path/summary pointing at it.

#### Scenario: Human review branch
- **WHEN** `openspec_plan` runs with `humanApproval` true
- **THEN** a `HUMAN` task presents the generated artifacts and captures `approved`/`feedback`, exactly as `design_docs.json`'s `design_review` task does today

#### Scenario: Human review draft includes proposal.md verbatim
- **WHEN** the human review branch's `WAIT` task (`plan_review`) constructs its `draft`
- **THEN** the draft's `inputParameters` include the full, unmodified text content of the generated `proposal.md`, not merely its path or a summary sentence
- **AND** the reviewer can read the actual proposal without leaving the review UI to open a file

#### Scenario: TUI approval modal renders the embedded proposal text
- **WHEN** the TUI's `ApprovalModal` displays a draft of kind `openspec_plan`
- **THEN** it renders the embedded `proposal.md` content as part of the draft display, in addition to the existing `changeDir`/`filesChanged`/`summary` fields

#### Scenario: AI-judge review branch
- **WHEN** `openspec_plan` runs with `humanApproval` false (or unset)
- **THEN** a read-only AI-judge `coding_agent` task judges the generated artifacts against the instruction and returns `{approved, feedback}` via structured output, exactly as `design_docs.json`'s `design_judge` task does today

#### Scenario: Rejection triggers another generation pass
- **WHEN** either review branch returns `approved: false`
- **AND** the iteration cap (`openspecMaxIterations`) has not been reached
- **THEN** `openspec_plan` regenerates the artifacts addressing the feedback and reviews again

#### Scenario: Exhausting the iteration cap without approval fails closed
- **WHEN** the iteration cap is reached without approval
- **THEN** `openspec_plan` terminates the run with the last reviewer feedback in `workflowOutput`, matching `design_docs.json`'s existing `design_not_approved` termination pattern

#### Scenario: Approval proceeds to task decomposition
- **WHEN** either review branch returns `approved: true`
- **THEN** `openspec_plan` proceeds to parse the generated `tasks.md` into `subtasks[]`
