## ADDED Requirements

### Requirement: OpenSpec-driven planning sub-workflow
`code_parallel.json` SHALL delegate design/spec planning to an `openspec_plan` Conductor sub-workflow that drives the `openspec` CLI, replacing `design_docs.json` and the freeform `plan` step.

#### Scenario: code_parallel invokes openspec_plan instead of design_docs and plan
- **WHEN** a `code_parallel` run starts with planning enabled
- **THEN** the workflow invokes the `openspec_plan` sub-workflow in place of the old `design_docs` SUB_WORKFLOW task and the old `plan` SIMPLE task
- **AND** neither `design_docs.json` nor the old freeform `plan` step's structured-output prompt is referenced anywhere in `code_parallel.json`

### Requirement: Deterministic CLI-driven scaffolding
`openspec_plan` SHALL create the OpenSpec change and read each artifact's generation instructions via typed Conductor tasks that invoke the `openspec` CLI, not via an agent deciding what shell commands to run.

#### Scenario: change scaffold created via typed task
- **WHEN** `openspec_plan` starts for a given instruction
- **THEN** a typed Conductor task runs `openspec new change <name>` against the target repo and returns the change's artifact paths
- **AND** no coding agent is invoked to decide whether or how to scaffold the change

#### Scenario: per-artifact instructions fetched via typed task
- **WHEN** `openspec_plan` needs to author a given artifact (proposal, specs, design, or tasks)
- **THEN** a typed Conductor task runs `openspec instructions <artifact> --change <name> --json` and returns the artifact's template, instruction text, and resolved output path
- **AND** the coding agent that authors the artifact content receives that returned instruction/template as its prompt input, rather than inventing its own structure

### Requirement: Dependency-ordered artifact sequencing
`openspec_plan` SHALL sequence artifact generation (proposal → specs/design → tasks) according to the dependency graph reported by `openspec status`, not according to agent judgment.

#### Scenario: status drives next artifact selection
- **WHEN** `openspec_plan` has completed one or more artifacts
- **THEN** it calls `openspec status --change <name> --json` and selects the next artifact(s) to generate from those reported as `ready` (dependencies satisfied)
- **AND** it does not generate an artifact whose dependencies are reported as `blocked`

#### Scenario: planning completes when apply-required artifacts are done
- **WHEN** every artifact ID in `openspec status`'s `applyRequires` has `status: "done"`
- **THEN** `openspec_plan` stops generating artifacts for that pass and proceeds to the review loop

### Requirement: Human-or-AI-judge review loop, retargeted from design_docs.json
`openspec_plan` SHALL reuse `design_docs.json`'s existing human-or-AI-judge `DO_WHILE` review loop unchanged in mechanism, applied to the generated OpenSpec artifacts instead of `docs/design/*.md`.

#### Scenario: human review branch
- **WHEN** `openspec_plan` runs with `humanApproval` true
- **THEN** a `HUMAN` task presents the generated artifacts and captures `approved`/`feedback`, exactly as `design_docs.json`'s `design_review` task does today

#### Scenario: AI-judge review branch
- **WHEN** `openspec_plan` runs with `humanApproval` false (or unset)
- **THEN** a read-only AI-judge `coding_agent` task judges the generated artifacts against the instruction and returns `{approved, feedback}` via structured output, exactly as `design_docs.json`'s `design_judge` task does today

#### Scenario: rejection triggers another generation pass
- **WHEN** either review branch returns `approved: false`
- **AND** the iteration cap (`openspecMaxIterations`) has not been reached
- **THEN** `openspec_plan` regenerates the artifacts addressing the feedback and reviews again

#### Scenario: exhausting the iteration cap without approval fails closed
- **WHEN** the iteration cap is reached without approval
- **THEN** `openspec_plan` terminates the run with the last reviewer feedback in `workflowOutput`, matching `design_docs.json`'s existing `design_not_approved` termination pattern

#### Scenario: approval proceeds to task decomposition
- **WHEN** either review branch returns `approved: true`
- **THEN** `openspec_plan` proceeds to parse the generated `tasks.md` into `subtasks[]`
