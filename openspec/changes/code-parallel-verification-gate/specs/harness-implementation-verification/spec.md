## ADDED Requirements

### Requirement: Bounded verification loop after fan-out
`code_parallel.json` SHALL run a bounded `DO_WHILE` verification loop after `merge_worktrees` and before handing off to a caller, and SHALL NOT proceed past that loop until it reports `passed: true` or the iteration cap is reached.

#### Scenario: Verification loop runs after merge
- **WHEN** `merge_worktrees` completes for a `code_parallel` run
- **THEN** a `verify_loop` `DO_WHILE` task runs before any subsequent output-producing step
- **AND** no PR-facing output (a branch ready for push) is produced until the loop reports `passed: true`

#### Scenario: Loop is bounded and fails closed
- **WHEN** the verification loop's iteration count reaches its configured maximum without ever reporting `passed: true`
- **THEN** `code_parallel.json` terminates the run without producing a mergeable/pushable result
- **AND** the termination reason includes the last round's test and judge findings

### Requirement: Verification combines real test execution and a semantic judge
Each pass of the verification loop SHALL (a) execute every subtask's declared `Test:` command for real against the merged worktree, and (b) run a read-only semantic judge comparing the merged diff against `proposal.md`, `design.md`, `specs/**`, and `tasks.md`. The loop SHALL report `passed: true` only when both checks pass.

#### Scenario: Declared test commands are actually executed
- **WHEN** a verification pass runs
- **THEN** every subtask's `testCmd` (as parsed from `tasks.md` by `common/tasks_md.py`) is executed against the merged worktree by a typed Conductor task, not merely mentioned in a coding agent's prompt
- **AND** the pass/fail result and output of each command is captured for that round

#### Scenario: Semantic judge evaluates spec fidelity
- **WHEN** a verification pass runs
- **THEN** a read-only `coding_agent` judge (modeled on `openspec_semantic_verify`) inspects the merged diff against the change's `proposal.md`, `design.md`, `specs/**`, and `tasks.md`
- **AND** it returns a structured `{passed, findings}` result without editing any files

#### Scenario: Both checks must pass
- **WHEN** either the declared test commands or the semantic judge report failure
- **THEN** the verification pass as a whole reports `passed: false`, even if the other check passed

### Requirement: Single consolidated fixup on failure
When a verification pass reports `passed: false` and the iteration cap has not been reached, the loop SHALL run exactly one consolidated fixup `coding_agent` against the already-merged worktree, not a re-fork of the original parallel subtasks, before re-running verification.

#### Scenario: Fixup receives failure context
- **WHEN** a verification pass fails
- **THEN** the fixup `coding_agent` invocation's prompt includes the failing test command output and the semantic judge's findings from that round
- **AND** it operates on the single merged worktree, not a newly created per-subtask worktree

#### Scenario: Fixup triggers re-verification, not re-planning
- **WHEN** the fixup `coding_agent` completes
- **THEN** the loop re-runs the same test-execution and semantic-judge checks against the fixed worktree
- **AND** it does not re-invoke `openspec_plan` or re-fork the original `subtasks[]`

### Requirement: Test-command tool access is explicit, not inherited from the default allowlist alone
Any `coding_agent` invocation that must run a subtask's declared `Test:` command (the original per-subtask coding agent and the fixup coding agent) SHALL receive an `allowedTools` list that is the harness's default allowed tools plus a Bash pattern derived from that command, computed explicitly at the call site — never just the derived pattern alone.

#### Scenario: Explicit allowlist preserves the defaults
- **WHEN** a subtask's or the fixup's `coding_agent` call is constructed with an `allowedTools` input
- **THEN** that list contains every entry in `common/tool_policy.py`'s `DEFAULT_ALLOWED_TOOLS`
- **AND** it additionally contains a `Bash(<token> *)` pattern derived from the first whitespace-delimited token of that subtask's declared `Test:` command

#### Scenario: Default-covered test commands need no extra pattern
- **WHEN** a subtask's declared `Test:` command's first token is already covered by `DEFAULT_ALLOWED_TOOLS` (e.g. `pytest`, `npm`, `npx`, `go`, `cargo`)
- **THEN** the computed `allowedTools` list is equivalent in effect to the default list, and the command runs without a permission denial

#### Scenario: Compound commands are a known, documented limitation
- **WHEN** a subtask's declared `Test:` command chains multiple invocations (e.g. via `&&`) or begins with `cd`
- **THEN** the first-token derivation MAY fail to allow the full command, and this is a documented constraint on how `Test:` commands are authored rather than a defect this requirement resolves
