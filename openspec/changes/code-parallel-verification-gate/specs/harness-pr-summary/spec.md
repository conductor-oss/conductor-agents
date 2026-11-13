## ADDED Requirements

### Requirement: PR body is composed deterministically from real run data
`issue_to_pr.json` and `address_pr.json` SHALL compose their PR body via a deterministic `JSON_JQ_TRANSFORM` task from the completed `code_parallel` run's actual data — the OpenSpec change's `proposal.md` text, the subtask list, the verification loop's findings, and cost/token totals — rather than a hardcoded string template or an LLM summarization call.

#### Scenario: PR body includes proposal content
- **WHEN** `issue_to_pr.json` or `address_pr.json` constructs its final PR body
- **THEN** the body includes the `proposal.md` text from the OpenSpec change that `code_parallel` planned and implemented for that run

#### Scenario: PR body includes subtask list
- **WHEN** the PR body is composed
- **THEN** it lists the subtasks `code_parallel` fanned out (as already available from `build_forks`/`aggregate` output), including at minimum each subtask's id and description

#### Scenario: PR body includes verification findings
- **WHEN** the PR body is composed
- **THEN** it includes the verification loop's final-round findings (test results and semantic judge findings) from `harness-implementation-verification`

#### Scenario: PR body includes cost and token totals
- **WHEN** the PR body is composed
- **THEN** it includes the aggregate cost and token totals already computed by `code_parallel.json`'s `aggregate` task

#### Scenario: Composition is a deterministic transform, not an LLM call
- **WHEN** the PR body is assembled
- **THEN** the assembling task is a `JSON_JQ_TRANSFORM` (or equivalent deterministic templating task) operating on already-produced workflow data
- **AND** no additional `coding_agent` call is made solely to write PR body prose

### Requirement: Hardcoded terse PR bodies are replaced
The existing hardcoded one-line PR body strings in `issue_to_pr.json`'s `final_pr` transform, `address_pr.json`'s revision body, and `openspec_finalize`'s default `body` output SHALL be replaced by the composed body defined in this capability wherever those workflows produce a PR-facing body.

#### Scenario: issue_to_pr no longer hardcodes a one-line body
- **WHEN** `issue_to_pr.json`'s `final_pr` step runs for the non-human-approved path
- **THEN** its `autoBody` is the composed body from this capability, not the prior hardcoded `"Closes #N\n\nAutomated resolution..."` string

#### Scenario: Human-approved path may still override
- **WHEN** `approvePr` is true and a human edits the PR body via the `pr_gate` `WAIT` task
- **THEN** the human's edited body is still honored as the final PR body, with the composed body serving as the draft they review and may edit
