## Purpose

Deterministic conversion of a generated `tasks.md` into the independent, file-disjoint `subtasks[]` array that `code_parallel.json`'s `FORK_JOIN_DYNAMIC` fan-out consumes, replacing the old freeform LLM-decomposition step.

## Requirements

### Requirement: Parallel-safe tasks.md generation
The `tasks` artifact instruction SHALL require that generated task groups be independent of one another and file-disjoint, and that each group declare its target files and test command.

#### Scenario: tasks.md groups declare files and test command
- **WHEN** the `tasks` artifact is generated for a `code_parallel` run
- **THEN** each numbered task group in `tasks.md` lists the files it will touch and a command to verify it
- **AND** no file appears in more than one group's file list

### Requirement: Deterministic tasks.md-to-subtasks parsing
`code_parallel.json` SHALL derive its `subtasks[]` fan-out list (`id`, `description`, `files`, `testCmd`) from the generated `tasks.md` via a deterministic parser, not via an LLM structured-output call.

#### Scenario: parser produces one subtask per independent task group
- **WHEN** `tasks.md` has been generated and approved
- **THEN** a deterministic parsing task reads `tasks.md` and emits one `subtasks[]` entry per numbered task group, using the group's heading slug as `id`, its task bullets as `description`, its declared files as `files`, and its declared verification command as `testCmd`
- **AND** this parsed `subtasks[]` array is passed into `build_forks` in place of `plan.output.structured.subtasks`

#### Scenario: parser rejects overlapping file ownership
- **WHEN** the parser detects that two task groups declare an overlapping file
- **THEN** the `openspec_plan` sub-workflow fails closed with a clear error identifying the conflicting groups and files, instead of proceeding to fan out subtasks that could collide

### Requirement: Unchanged downstream fan-out
`code_subtask.json`, the `FORK_JOIN_DYNAMIC` fan-out, and `merge_worktrees` SHALL continue to operate on the `subtasks[]` shape unchanged, regardless of whether it originated from the old `plan` step or the new tasks.md parser.

#### Scenario: fan-out is agnostic to subtask origin
- **WHEN** `build_forks` receives a `subtasks[]` array shaped as `{id, description, files, testCmd}`
- **THEN** it builds the same `dynamicTasks`/`dynamicTasksInput` structure regardless of whether that array came from the retired `plan` step or the new tasks.md parser
