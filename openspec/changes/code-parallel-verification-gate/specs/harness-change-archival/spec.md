## ADDED Requirements

### Requirement: code_parallel does not archive the OpenSpec change
`code_parallel.json` (and its callers `issue_to_pr.json`/`address_pr.json`, which use it as their implementation engine) SHALL NOT invoke `openspec archive` or otherwise move/complete the OpenSpec change directory as part of a run, regardless of whether the verification loop (see `harness-implementation-verification`) reports `passed: true`. Archiving the change is an out-of-band step a human performs after the PR merges; no workflow in this pipeline automates it.

#### Scenario: Successful verification does not trigger archive
- **WHEN** the verification loop reports `passed: true`
- **THEN** `code_parallel.json` hands its verified, merged worktree back to its caller (for `git_push`/`pr_create`) without archiving the OpenSpec change
- **AND** the OpenSpec change directory remains under `openspec/changes/<id>/`, unarchived, and is included in the resulting PR's diff

#### Scenario: Failed verification also does not archive
- **WHEN** the verification loop terminates without ever reporting `passed: true`
- **THEN** no archive step runs (as before), and the OpenSpec change directory remains un-archived under `openspec/changes/`

### Requirement: Superseded bespoke validate/archive tasks are removed
The `openspec_change_validation` and `openspec_archive` worker tasks (`coding-harness/workers/openspecops/tasks.py`) and their `validate_changes`/`archive` CLI wrapper functions (`coding-harness/workers/common/openspec_cli.py`) SHALL be removed: they duplicate `openspec_finalize`'s proven sequence with an unverified argument shape and were never wired into any workflow.

#### Scenario: Bespoke tasks are absent
- **WHEN** this change is complete
- **THEN** `openspecops/tasks.py` no longer defines `openspec_change_validation` or `openspec_archive` task handlers
- **AND** `common/openspec_cli.py` no longer defines `validate_changes` or `archive` functions

### Requirement: openspec_finalize is unchanged and unused by this pipeline
`openspec_finalize` (`coding-harness/workers/openspec/tasks.py`) SHALL remain exactly as it exists today, serving only `openspec_development.json`'s separate verify/finalize gate. `code_parallel.json` SHALL NOT call it, directly or via a sub-workflow.

#### Scenario: openspec_development keeps working
- **WHEN** `openspec_development.json` runs its existing `verified_gate` → `openspec_finalize` path
- **THEN** its behavior is unaffected by this change

#### Scenario: code_parallel never calls openspec_finalize
- **WHEN** `code_parallel.json` (or `issue_to_pr.json`/`address_pr.json`) runs to completion, successfully or not
- **THEN** `openspec_finalize` is never invoked as part of that run
