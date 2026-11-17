## ADDED Requirements

### Requirement: Issue body is fetched verbatim from the GitHub API
The `issue_fetch` step SHALL retrieve the GitHub issue's `title` and `body` in full from the GitHub API and forward them without truncation, summarization, or content-altering escaping. Any truncation applied at the fetch/log stage MUST be cosmetic (display-only) and MUST NOT affect the values forwarded to downstream tasks.

#### Scenario: Full body retrieved via gh
- **WHEN** `issue_fetch` runs `gh issue view <number> --repo <slug> --json number,title,body,state,url,labels` (`common/github.py:89-104`)
- **THEN** the returned `body` field equals the complete issue body as returned by the GitHub API, byte-for-byte

#### Scenario: Log truncation does not affect forwarded data
- **WHEN** the task summary truncates the title for logging via `title[:80]` (`gitops/tasks.py`)
- **THEN** the `title` and `body` values passed to downstream tasks remain the full, untruncated strings

### Requirement: Issue title and body are assembled into the planner instruction without loss
The `issue_to_pr` workflow SHALL interpolate `${issue.output.title}` and `${issue.output.body}` into the `instruction` string (`issue_to_pr.json:60`) such that the complete title and body appear in the assembled instruction, and that instruction SHALL flow unchanged through `code_parallel` into `openspec_plan`.

#### Scenario: Instruction contains complete title and body
- **WHEN** the `code_parallel` sub-workflow is invoked with the assembled `instruction`
- **THEN** the instruction string contains the full issue title and the full issue body exactly as fetched

#### Scenario: Instruction is not re-truncated in transit
- **WHEN** `instruction` is forwarded from `issue_to_pr` through `code_parallel` to `openspec_plan`
- **THEN** no intermediate task shortens, re-wraps, or drops any portion of the instruction

### Requirement: Instruction reaches the OpenSpec planner intact on both paths
The instruction SHALL reach the planner on both wiring paths without content loss: (a) as the `description` input to `openspec_new_change` (`openspec_plan.json:25` → `openspecops/tasks.py:23` → `common/openspec_cli.py:43-47`, passed as a single `--description <body>` argument), and (b) as the `goal` embedded in the artifact-writer agent prompt (`openspec_plan.json:41` → `openspec_generate_artifact.json:28`).

#### Scenario: Description path passes the full body as one argument
- **WHEN** `new_change` is called with a non-empty description (`common/openspec_cli.py:43-47`)
- **THEN** the entire description is passed as a single `--description` command-line argument to `openspec new change`

#### Scenario: Goal path embeds the full instruction in the agent prompt
- **WHEN** the artifact-drain loop constructs the artifact-writer prompt (`openspec_generate_artifact.json:28`)
- **THEN** the embedded `goal` contains the complete instruction that drives artifact content generation

### Requirement: Known ingestion risk points are identified and assessed
The investigation SHALL enumerate and assess every candidate loss point that is not hard-capped today but could still corrupt or drop content, distinguishing silent truncation from hard failure. The assessed risks MUST include: `openspec new change --description` potentially treating the description as a short summary/first line when seeding `proposal.md`; Conductor re-interpolation of `${...}`/dollar-brace sequences appearing inside the issue body; Conductor task input/output payload-size limits on large bodies; JSON-string escaping of quotes, backslashes, and newlines during template substitution; and OS `ARG_MAX`/per-argument `MAX_ARG_STRLEN` limits when a large body is passed as one CLI argument.

#### Scenario: Each risk point is classified
- **WHEN** the report documents a candidate loss point
- **THEN** it states the `file:line` location, whether the failure mode is silent truncation/corruption or a hard error, and the assessed likelihood

#### Scenario: Silent truncation risks are highlighted
- **WHEN** a risk can drop content without surfacing an error (e.g. `--description` seeding only a summary line)
- **THEN** the report explicitly flags it as a silent failure requiring downstream verification

### Requirement: Investigation deliverable is a written report with traced data flow
This change SHALL deliver a written markdown report that traces the full path of a GitHub issue's title and body from GitHub API fetch to the OpenSpec planner, with concrete `file:line` references at each hop, and states clear findings and follow-up recommendations. The change SHALL make no code changes.

#### Scenario: Report traces every hop with file references
- **WHEN** a reader follows the report from `issue_fetch` to `openspec new change`
- **THEN** every hop (fetch, instruction assembly, sub-workflow forwarding, description path, goal path) is documented with a `file:line` reference

#### Scenario: Report ends with findings and recommendations
- **WHEN** the report is complete
- **THEN** it lists concrete findings and follow-up recommendations (verification tests, guardrails, or a hardened ingestion path)

#### Scenario: No source code is modified
- **WHEN** the change is delivered
- **THEN** only the report and this OpenSpec change's artifacts are added, and no runtime, API, or dependency code is changed
