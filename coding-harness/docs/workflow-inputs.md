# Workflow input reference

This is the complete input contract for every registered harness workflow (version 1).
It is derived from the workflow definitions in `workers/workflows/`: an input is **required**
when it is listed in `inputParameters` and has no value in `inputTemplate`; every other listed
input is **optional**, with the shown default. Internal workflows are included for operators and
tests, but normally only the user-facing workflows are started directly.

Empty strings mean “use the normal runtime default” for that field. Model fields are blank by
default so the selected model profile can resolve them; do not replace them with a provider
default unless you are deliberately overriding the profile. `*PromptTemplateSource` records the
provenance of its companion override.

The following optional policy envelope is available on **every** workflow and is listed here once
rather than repeated in every optional-parameter line below:

- `modelProfile` defaults to `""`; pass a profile name (for example,
  `anthropic-standard` or `openai-standard`) or leave it blank for the configured default.
- `modelPolicy` defaults to `{}` and carries the selected one-file user-policy snapshot.
- `modelPolicySource`, `modelPolicySha256`, and `modelsConfig` default to `""`; they record the
  snapshot provenance and optionally select a contained repository policy path.
- `modelOverrides` defaults to `{}` for explicit structured role overrides.

Every workflow resolves this envelope before agent work. The resulting role tier is carried into
sub-workflows, dynamic forks, and scheduled children; workers record the selected profile, role,
model, and source hashes in their task output. A nonblank legacy backend/model field remains an
explicit override for that task role.

## `address_pr`

Required: `repo`, `prNumber`.

Optional: `engine` = `"code_parallel"`; `agent` = `"claude"`; `model` = `""`; `design` = `false`; `designHumanApproval` = `true`; `designMaxIterations` = `5`; `fixPromptTemplate` = `""`; `fixPromptTemplateSource` = `""`; `maxSubtasks` = `4`; `maxTurns` = `250`; `maxBudgetUsd` = `50.0`.

## `automation_dispatch` (internal)

Required: `repo`, `kind`, `childWorkflow`, `number`, `revision`, `attempt`.

Optional: `approvalMode` = `"human"`; `agent` = `"claude"`; `model` = `""`; `judgeAgent` = `"claude"`; `judgeModel` = `""`; `judgeMaxTurns` = `50`; `judgeMaxBudgetUsd` = `5`; `maxApprovalRevisions` = `2`; `verificationProfile` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `fixPromptTemplate` = `""`; `fixPromptTemplateSource` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `designJudgePromptTemplate` = `""`; `designJudgePromptTemplateSource` = `""`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `approvalJudgePromptTemplate` = `""`; `approvalJudgePromptTemplateSource` = `""`.

## `automation_reset` (internal)

Required: `repo`, `kind`, `number`, `revision`. Optional: none.

## `campaign_subtask` (internal)

Required: `repoPath`, `task`, `wave`.

Optional: `agent` = `"claude"`; `model` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`; `resumeSessionId` = `""`; `feedback` = `""`; `specContextPath` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`.

## `code_parallel`

Required: `repoPath`, `instruction`.

Optional: `changeBranch` = `"code-parallel"`; `design` = `false`; `designAgent` = `"claude"`; `designModel` = `""`; `designDir` = `"docs/design"`; `designMaxTurns` = `500`; `designHumanApproval` = `true`; `designMaxIterations` = `5`; `planAgent` = `"claude"`; `planModel` = `""`; `planMaxTurns` = `500`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `codeAgent` = `"claude"`; `codeModel` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `maxSubtasks` = `6`; `maxTurns` = `500`; `maxBudgetUsd` = `50.0`; `precomputedPlan` = `{}`; `specContextPath` = `""`; `usePrecomputedPlan` = `false`.

## `code_revision_loop` (internal)

Required: `worktreePath`, `workflowId`, `loopId`, `prompt`, `modelResolution`, `bestCommit`.

Optional: `promptTemplate` = `""`; `promptTemplateSource` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50.0`; `checks` = `[]`; `findings` = `[]`; `accepted` = `false`; `round` = `1`; `maxRounds` = `8`; `plateauCount` = `0`.

## `code_subtask` (internal)

Required: `repoPath`, `name`, `prompt`, `model`, `agent`.

Optional: `promptTemplate` = `""`; `promptTemplateSource` = `""`; `templateKey` = `"code"`; `promptContext` = `{}`; `maxTurns` = `250`; `maxBudgetUsd` = `50.0`; `specContextPath` = `""`.

## `design_docs`

Required: `repoPath`, `instruction`.

Optional: `designDir` = `"docs/design"`; `designAgent` = `"claude"`; `designModel` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `designMaxTurns` = `500`; `designMaxBudgetUsd` = `50.0`; `humanApproval` = `true`; `designMaxIterations` = `5`.

## `feature_campaign`

Required: `repoPath`, `instruction`.

Optional: `workspacePath` = `""`; `keepWorktree` = `true`; `changeBranch` = `""`; `designDir` = `"docs/design"`; `designAgent` = `"claude"`; `designModel` = `""`; `planAgent` = `"claude"`; `planModel` = `""`; `codeAgent` = `"claude"`; `codeModel` = `""`; `reviewAgent` = `"claude"`; `reviewModel` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`; `maxTasks` = `25`; `maxParallelism` = `6`; `maxWaves` = `20`; `designMaxRevisions` = `5`; `planMaxRevisions` = `5`; `checksConfig` = `".conductor-code/checks.json"`; `waveProfile` = `""`; `finalProfile` = `""`; `useImportedPlan` = `false`; `importedPlan` = `{}`; `importedDesignLocation` = `""`; `specContextPath` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `revisionPromptTemplate` = `""`; `revisionPromptTemplateSource` = `""`.

## `github_demo`

Required: `repoUrl`, `instruction`.

Optional: `changeBranch` = `"conductor-harness-change"`; `base` = `""`; `agent` = `"claude"`; `model` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `prTitle` = `""`; `maxTurns` = `300`; `maxBudgetUsd` = `50.0`.

## `issue_resolution_sweep`

Required: `repo`.

Optional: `issueLabel` = `"conductor:auto"`; `approvalMode` = `"human"`; `agent` = `"claude"`; `model` = `""`; `judgeAgent` = `"claude"`; `judgeModel` = `""`; `judgeMaxTurns` = `50`; `judgeMaxBudgetUsd` = `5`; `maxApprovalRevisions` = `2`; `verificationProfile` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `designJudgePromptTemplate` = `""`; `designJudgePromptTemplateSource` = `""`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `approvalJudgePromptTemplate` = `""`; `approvalJudgePromptTemplateSource` = `""`; `maxNew` = `1`; `maxActive` = `1`.

## `issue_to_pr`

Required: `repo`, `issueNumber`.

Optional: `base` = `"main"`; `planAgent` = `"claude"`; `codeAgent` = `"claude"`; `designAgent` = `"claude"`; `design` = `false`; `designHumanApproval` = `true`; `designMaxIterations` = `5`; `approvePr` = `false`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `maxSubtasks` = `4`; `maxTurns` = `300`; `maxBudgetUsd` = `50.0`.

## `local_review`

Required: `repoPath`.

Optional: `baseRemote` = `"origin"`; `baseBranch` = `"main"`; `agent` = `"claude"`; `model` = `""`; `localReviewPromptTemplate` = `""`; `localReviewPromptTemplateSource` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50`.

## `openspec_development`

Required: `specSource`, `changeId`. The target path is required unless the local-source-workspace option is enabled for an absolute local Git-backed source.

Optional: `repoPath` = `""`; `useSpecSourceWorkspace` = `false`; `workspacePath` = `""`; `keepWorktree` = `true`; `specSourceType` = `"auto"`; `specRef` = `""`; `specPath` = `""`; `specWritebackRepo` = `""`; `specWritebackRef` = `""`; `executionMode` = `"auto"`; `instruction` = `""`; `changeBranch` = `""`; `archiveBranch` = `""`; `base` = `"main"`; `agent` = `"claude"`; `model` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`; `maxTasks` = `25`; `maxParallelism` = `6`; `maxWaves` = `20`; `checksConfig` = `".conductor-code/checks.json"`; `finalProfile` = `""`; `assessPromptTemplate` = `""`; `assessPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `verificationPromptTemplate` = `""`; `verificationPromptTemplateSource` = `""`.

## `pr_address_sweep`

Required: `repo`.

Optional: `approvalMode` = `"human"`; `agent` = `"claude"`; `model` = `""`; `judgeAgent` = `"claude"`; `judgeModel` = `""`; `judgeMaxTurns` = `50`; `judgeMaxBudgetUsd` = `5`; `maxApprovalRevisions` = `2`; `verificationProfile` = `""`; `fixPromptTemplate` = `""`; `fixPromptTemplateSource` = `""`; `approvalJudgePromptTemplate` = `""`; `approvalJudgePromptTemplateSource` = `""`; `maxNew` = `2`; `maxActive` = `2`.

## `pr_review`

Required: `repo`, `prNumber`.

Optional: `agent` = `"claude"`; `model` = `""`; `approve` = `false`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50.0`.

## `pr_review_sweep`

Required: `repo`.

Optional: `approvalMode` = `"human"`; `agent` = `"claude"`; `model` = `""`; `judgeAgent` = `"claude"`; `judgeModel` = `""`; `judgeMaxTurns` = `50`; `judgeMaxBudgetUsd` = `5`; `maxApprovalRevisions` = `2`; `verificationProfile` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `approvalJudgePromptTemplate` = `""`; `approvalJudgePromptTemplateSource` = `""`; `maxNew` = `5`; `maxActive` = `5`.
