# Workflow input reference

This is the complete input contract for every harness workflow definition in this repository.
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
  `anthropic-standard` or `openai-standard`) or leave it blank for the configured default. TUI
  launches snapshot `/models` in that case; raw API/CLI callers retain bundled/repository defaulting.
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

Optional: `agent` = `""`; `design` = `false`; `designDir` = `"docs/design"`; `designHumanApproval` = `true`; `designMaxIterations` = `5`; `designMaxTurns` = `250`; `designMaxBudgetUsd` = `50`; `designModel` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `engine` = `"code_parallel"`; `fixPromptTemplate` = `""`; `fixPromptTemplateSource` = `""`; `maxApprovalRevisions` = `2`; `maxBudgetUsd` = `50`; `maxFixAttempts` = `4`; `maxSubtasks` = `4`; `maxTurns` = `250`; `model` = `""`; `openspecPlanAgent` = `""`; `openspecPlanModel` = `""`; `openspecMaxTurns` = `500`; `openspecMaxBudgetUsd` = `50`; `openspecHumanApproval` = `true`; `openspecMaxIterations` = `5`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`.

`allowAgentTestPlan`/`allowAgentAuthoredTests` are forwarded to both the initial `verify` pass and the `address_pr_repair` sub-workflow's own `test_cycle` call, so a PR-comment fix that touches a file with no directly-matching test (a shared helper, for example) is not permanently `configuration_blocked` — see `test_cycle`'s entry below. Both default `true` here (unlike `test_cycle`/`test_agent_fallback`'s own default of `false`) since this is the interactive PR-feedback loop where leaving a change permanently blocked on missing test-file mapping is worse than trying the agent fallback first; set either to `false` to restore the strict deterministic-only behavior.

## `address_pr_repair` (internal)

Required: `repoPath`, `candidateCommit`, `verification`.

Optional: `agent` = `""`; `model` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50`; `fixPromptTemplate` = `""`; `fixPromptTemplateSource` = `""`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`. This workflow returns remediation evidence only; it cannot push or comment. Defaults match `address_pr`, which is `address_pr_repair`'s only caller and always forwards its own values explicitly; these defaults matter only for a standalone/direct invocation.

## `address_pr_approval` (internal)

Required: `repo`, `prNumber`, `repoPath`, `workspacePath`, `branch`, `candidateCommit`.

Optional: `feedback` = `""`; `verificationState` = `"passed"`; `verification` = `{}`; `replyBody` = `""`; `agentResult` = `""`; `subtasks` = `[]`; `totalTokens` = `0`; `maxApprovalRevisions` = `2`; `agent` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50`; `maxSubtasks` = `4`.

`address_pr`'s human-approval gate for one already-verified candidate, extracted to its own sub-workflow to keep `address_pr`'s own graph small. A verified candidate is never auto-published: the `address_gate` `WAIT` task always pauses for an explicit human `approve`/`revise`/`stop` decision, offering up to `maxApprovalRevisions` bounded, independently re-verified revision rounds first (each `revise` reruns `code_parallel`, marked `optional: true` so an infra failure there degrades to `verification_blocked` rather than failing the whole `address_pr` run). Resolves `approvalState` to exactly one of: `approved`, `suppressed`, `blocked`, `verification_blocked`, `revision_exhausted` (a still-`pending` state when `maxApprovalRevisions` is exhausted). `address_pr`'s `publication_gate` only publishes when this resolves to `approved`.

## `test_agent_fallback` (internal)

Required: `repoPath`, `candidateCommit`, `discoveryOutcome`.

Optional: `testMode` = `"targeted"`; `allowHeavySuites` = `false`; `includeBrowserTests` = `false`; `allowAgentTestPlan` = `false`; `allowAgentAuthoredTests` = `false`; `discoveryReason` = `""`; `changedPaths` = `[]`; `repairRoots` = `[]`; `agent` = `""`; `model` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50`.

`test_cycle`'s agent-assisted discovery/authoring fallback, extracted to its own sub-workflow to keep `test_cycle`'s own graph small. A no-op that echoes `candidateCommit`/`repairRoots` straight back unless `allowAgentTestPlan` is `true` and `discoveryOutcome` is `configuration_blocked`. Tier 1: a read-only coding agent's proposed test plan, validated against this module's strictest gate (`validate_agent_argv`: the entrypoint must belong to a build system the repository actually shows evidence of, every path-like token must resolve to a real file, every claimed covered path must be a real changed path, and every changed file that looks like a test must be covered by some proposal). Tier 2, only reached when tier 1 also finds nothing and `allowAgentAuthoredTests` is `true`: a coding agent (pinned to the Claude backend, tools limited to `Read`/`Grep`/`Glob`/`Write`/`Edit` — no `Bash`) may author exactly one new test file, discovering the repository's own test convention itself rather than following any hardcoded per-language rule. The new file is never trusted on the agent's say-so: it must be the single new, test-shaped file (nothing else touched), must textually reference the changed code, and must pass a red/green check — failing when run against the candidate's pre-change baseline and passing against the real candidate change — before it is committed and treated as the resolved test plan. `agentAuthoredTest`/`agentAuthoredTestPath` in the output report whether this happened. It owns no branch: an accepted authored test is committed to whatever branch `repoPath` is already on.

## `test_cycle` (internal)

Required: `repoPath`, `candidateCommit`.

Optional: `testMode` = `"targeted"`; `maxFixAttempts` = `4`; `testCommands` = `[]`; `testPlanTemplate` = `""`; `testPlanTemplateSource` = `""`; `allowHeavySuites` = `false`; `includeBrowserTests` = `false`; `allowAgentTestPlan` = `false`; `allowAgentAuthoredTests` = `false`; `priorVerification` = `{}`; `allowedWriteRoots` = `[]`; `agent` = `""`; `model` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50`; `repairPromptTemplate` = `""`; `repairPromptTemplateSource` = `""`.

Runs the repository's tests for one exact candidate commit and fixes what fails. `testMode` = `"targeted"` derives an exact-test or affected-unit plan from the candidate's diff; `testMode` = `"full"` runs the repository-wide suite and is the mode to use before publishing a PR — full mode reruns the whole suite on every repair iteration, so reserve it for a final pre-publication gate, not a review-feedback loop. `allowHeavySuites` is honoured only in full mode and is what permits an integration or end-to-end target; it is never inferred. `includeBrowserTests` is also full-mode-only and adds the repository's dedicated Playwright/Cypress script when one exists — it never triggers a browser install: if binaries are not already provisioned on the host, this reports a diagnostic in `rejectedCandidates` rather than blocking the run. `testCommands` and `testPlanTemplate` are the two forms of the operator override and both outrank every other source: `testCommands` is a structured argv list; `testPlanTemplate` is the same shape as an inline JSON string, for a caller that only has text to pass, and is rejected outright if it starts with `@` (the templating convention for resolving from a file in the checkout, which the candidate controls) rather than falling back to it. When both are supplied, `testCommands` wins, since it cannot suffer a JSON-parsing surprise. Below the operator override, the precedence is repository guides, then `.conductor-code/verification.json`, then build-system inference, and — only when every one of those comes up empty — `test_agent_fallback` (see above), gated on `allowAgentTestPlan`/`allowAgentAuthoredTests`. The agent is invoked at most once per candidate SHA and is never re-proposed after a repair; its accepted plan is carried forward the same way a `priorVerification` obligation is. Repair writes are restricted to `allowedWriteRoots`, falling back to the discovered changed paths.

The budget is `maxFixAttempts` fixes and one more test run than that, capped at 4 and 5. Because each loop iteration is fix-then-verify, the final run is always a verdict and no fix is ever left unvalidated. The loop also stops early when a fix produces no new commit, since re-running would repeat the identical result.

**This workflow never fails.** Every terminal condition completes with a `testCycleState` the caller branches on: `tests_passed`, `no_tests_required`, `tests_failed_after_fix_budget`, `tests_failed_fix_unavailable`, `command_discovery_blocked`, `runtime_unavailable`, `candidate_commit_missing`, or `verifier_worker_unavailable`. `testsPassed` is the boolean for a SWITCH gate.

One caveat: never failing is not the same as always completing. There are no execution deadlines anywhere in this harness, so if the isolated verification worker is not polling, `test_discover` and `test_run` never start and the workflow stays RUNNING rather than reporting `verifier_worker_unavailable` — that state only fires when a task actually fails. A caller that needs a bounded answer must watch queue depth or `verification_health`, not this workflow.

It owns no branch: fixes are committed to whatever branch `repoPath` is checked out on.

## `merge_remediation` (internal)

Required: `repoPath`, `groupIds`.

Optional: `modelBuilder` = `""`; `maxBudgetUsd` = `50.0`; `preflight` = `false`.

## `publish_verified_pr` (internal)

Required: `repoPath`, `repo`, `headRepo`, `prNumber`, `branch`, `expectedHeadSha`, `candidateCommit`.

Optional: `replyBody` = `""`. The caller must approve and locally verify the candidate first. This workflow performs the one candidate guard, remote-head guard, exact commit push, bounded exact-SHA CI poll, and success comment.

## `automation_dispatch` (internal)

Required: `repo`, `kind`, `childWorkflow`, `number`, `revision`, `attempt`.

Optional: `childWorkflowVersion` = `1`; `approvalMode` = `"human"`; `agent` = `"claude"`; `model` = `""`; `judgeAgent` = `"claude"`; `judgeModel` = `""`; `judgeMaxTurns` = `50`; `judgeMaxBudgetUsd` = `5`; `maxApprovalRevisions` = `2`; `verificationProfile` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `fixPromptTemplate` = `""`; `fixPromptTemplateSource` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `designJudgePromptTemplate` = `""`; `designJudgePromptTemplateSource` = `""`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `approvalJudgePromptTemplate` = `""`; `approvalJudgePromptTemplateSource` = `""`.

## `automation_reset` (internal)

Required: `repo`, `kind`, `number`, `revision`. Optional: none.

## `campaign_subtask` (internal)

Required: `repoPath`, `task`, `wave`.

Optional: `agent` = `"claude"`; `model` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`; `resumeSessionId` = `""`; `feedback` = `""`; `specContextPath` = `""`; `contextPaths` = `[]`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`.

## `code_parallel`

Required: `repoPath`, `instruction`.

Optional: `branchRunId` = `""`; `changeBranch` = `""`; `openspecPlanModel` = `""`; `openspecMaxTurns` = `500`; `openspecMaxBudgetUsd` = `50`; `openspecHumanApproval` = `true`; `openspecMaxIterations` = `5`; `codeModel` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`; `maxFixAttempts` = `4`; `precomputedPlan` = `{}`; `specContextPath` = `""`; `usePrecomputedPlan` = `false`; `design` = `false`; `designDir` = `"docs/design"`; `designHumanApproval` = `true`; `designMaxIterations` = `5`; `designMaxTurns` = `500`; `designMaxBudgetUsd` = `50`; `designModel` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `inPlace` = `false`; `workspacePath` = `""`; `contextPaths` = `[]`.

After merging the subtask branches this runs `test_cycle` in targeted mode, so the candidate it hands back has been tested and, where possible, repaired. `maxFixAttempts` bounds that repair loop. The result is diagnostic: `testCycleState` and `testsPassed` are returned for the caller to gate on, and a red suite never withholds the candidate commit. Each subtask also runs its own targeted `test_cycle` with a single fix attempt before its branch is merged.

## `code_revision_loop` (internal)

Required: `worktreePath`, `workflowId`, `loopId`, `prompt`, `modelResolution`, `bestCommit`.

Optional: `promptTemplate` = `""`; `promptTemplateSource` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50.0`; `checks` = `[]`; `findings` = `[]`; `accepted` = `false`; `round` = `1`; `maxRounds` = `8`; `plateauCount` = `0`.

## `code_subtask` (internal)

Required: `repoPath`, `name`, `prompt`, `model`.

Optional: `promptTemplate` = `""`; `promptTemplateSource` = `""`; `templateKey` = `"code"`; `promptContext` = `{}`; `maxTurns` = `250`; `maxBudgetUsd` = `50.0`; `specContextPath` = `""`; `contextPaths` = `[]`; `allowedTools` = `[]`; `allowedWriteRoots` = `[]`.

## `design_docs` (internal)

Required: `instruction`, `repoPath`.

Optional: `designAgent` = `""`; `designDir` = `"docs/design"`; `designMaxBudgetUsd` = `50`; `designMaxIterations` = `5`; `designMaxTurns` = `500`; `designModel` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `humanApproval` = `true`; `contextPaths` = `[]`.

## `document_plan` (internal)

Required: `repoPath`, `instruction`.

Optional: `contextPaths` = `[]`; `model` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`.

## `openspec_plan` (internal)

Required: `repoPath`, `instruction`, `changeBranch`.

Optional: `openspecPlanModel` = `""`; `openspecMaxTurns` = `500`; `openspecMaxBudgetUsd` = `50.0`; `openspecHumanApproval` = `true`; `openspecMaxIterations` = `5`.

## `openspec_artifact_drain` (internal)

Required: `repoPath`, `changeName`, `goal`.

Optional: `feedback` = `""`; `model` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50.0`.

## `openspec_generate_artifact` (internal)

Required: `repoPath`, `changeName`, `artifact`, `goal`.

Optional: `feedback` = `""`; `model` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50.0`.

## `feature_campaign`

Required: `repoPath`, `instruction`.

Optional: `workspacePath` = `""`; `keepWorktree` = `true`; `changeBranch` = `""`; `inPlace` = `false`; `contextPaths` = `[]`; `createPr` = `false`; `prBase` = `"main"`; `prTitle` = `""`; `prBody` = `""`; `prDraft` = `false`; `designDir` = `"docs/design"`; `designAgent` = `"claude"`; `designModel` = `""`; `planAgent` = `"claude"`; `planModel` = `""`; `codeAgent` = `"claude"`; `codeModel` = `""`; `reviewAgent` = `"claude"`; `reviewModel` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`; `maxFixAttempts` = `4`; `maxTasks` = `25`; `maxParallelism` = `6`; `maxWaves` = `20`; `designMaxRevisions` = `5`; `planMaxRevisions` = `5`; `useImportedPlan` = `false`; `importedPlan` = `{}`; `importedDesignLocation` = `""`; `specContextPath` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `revisionPromptTemplate` = `""`; `revisionPromptTemplateSource` = `""`.

`prBody` is a summary source, not a free-form PR template. Publication normalizes it to one
`## Summary` paragraph; when empty, `instruction` supplies the summary.

## `github_demo`

Required: `repoUrl`, `instruction`.

Optional: `changeBranch` = `"conductor-harness-change"`; `base` = `""`; `agent` = `"claude"`; `model` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `prTitle` = `""`; `maxTurns` = `300`; `maxBudgetUsd` = `50.0`.

## `issue_resolution_sweep`

Required: `repo`.

Optional: `issueLabel` = `"conductor:auto"`; `approvalMode` = `"human"`; `agent` = `"claude"`; `model` = `""`; `judgeAgent` = `"claude"`; `judgeModel` = `""`; `judgeMaxTurns` = `50`; `judgeMaxBudgetUsd` = `5`; `maxApprovalRevisions` = `2`; `verificationProfile` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `designJudgePromptTemplate` = `""`; `designJudgePromptTemplateSource` = `""`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `approvalJudgePromptTemplate` = `""`; `approvalJudgePromptTemplateSource` = `""`; `maxNew` = `1`; `maxActive` = `1`.

## `issue_to_pr`

Required: `repo`, `issueNumber`.

Optional: `base` = `"main"`; `approvePr` = `false`; `design` = `false`; `designDir` = `"docs/design"`; `designHumanApproval` = `true`; `designMaxIterations` = `5`; `designMaxTurns` = `500`; `designMaxBudgetUsd` = `50`; `designModel` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `maxApprovalRevisions` = `2`; `openspecHumanApproval` = `true`; `openspecMaxIterations` = `5`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `maxTurns` = `300`; `maxBudgetUsd` = `50`; `allowAgentTestPlan` = `false`; `allowAgentAuthoredTests` = `false`.

`allowAgentTestPlan` and `allowAgentAuthoredTests` forward to the final full-suite `test_cycle` sub-workflow exactly as documented under `test_cycle` above; the resulting `agentAuthoredTest` forces `prDraft` to `true` as defense in depth even when delivery otherwise passed.

## `publish_salvage` (internal)

Required: `workflowId`, `reason`, `failureStatus`, `failureTaskId`, `failedWorkflow`.

Optional: none beyond the shared policy envelope. Conductor supplies every required input automatically when it invokes this as the `failureWorkflow` of `issue_to_pr` — it is not started by hand. When a run dies before reaching its publication gate, this pushes whatever branch survived and opens a draft PR carrying the failure reason, so failed work is delivered for review rather than stranded in a scratch worktree. PR creation is best-effort (`optional`): if it fails, the push still stands and the run reports `pushed_no_pr`.

## `local_review`

Required: `repoPath`.

Optional: `baseRemote` = `"origin"`; `baseBranch` = `"main"`; `agent` = `"claude"`; `model` = `""`; `localReviewPromptTemplate` = `""`; `localReviewPromptTemplateSource` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50`.

## `openspec_development`

Required: `specSource`, `changeId`. The target path is required unless the local-source-workspace option is enabled for an absolute local Git-backed source.

Optional: `repoPath` = `""`; `useSpecSourceWorkspace` = `false`; `workspacePath` = `""`; `keepWorktree` = `true`; `specSourceType` = `"auto"`; `specRef` = `""`; `specPath` = `""`; `specWritebackRepo` = `""`; `specWritebackRef` = `""`; `executionMode` = `"auto"`; `instruction` = `""`; `changeBranch` = `""`; `archiveBranch` = `""`; `base` = `"main"`; `agent` = `"claude"`; `model` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`; `maxTasks` = `25`; `maxParallelism` = `6`; `maxWaves` = `20`; `assessPromptTemplate` = `""`; `assessPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `verificationPromptTemplate` = `""`; `verificationPromptTemplateSource` = `""`.

## `pr_address_sweep`

Required: `repo`.

Optional: `approvalMode` = `"human"`; `agent` = `"claude"`; `model` = `""`; `judgeAgent` = `"claude"`; `judgeModel` = `""`; `judgeMaxTurns` = `50`; `judgeMaxBudgetUsd` = `5`; `maxApprovalRevisions` = `2`; `verificationProfile` = `""`; `fixPromptTemplate` = `""`; `fixPromptTemplateSource` = `""`; `approvalJudgePromptTemplate` = `""`; `approvalJudgePromptTemplateSource` = `""`; `maxNew` = `2`; `maxActive` = `2`.

## `pr_review`

Required: `repo`, `prNumber`.

Optional: `agent` = `""`; `model` = `""`; `approve` = `false`; `approvalMode` = `""`; `reviewGuidance` = `""`; `maxInvestigationPasses` = `5`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `reviewInvestigationPromptTemplate` = `""`; `reviewInvestigationPromptTemplateSource` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50`. A clean review posts only `LGTM` and APPROVEs. With `approve:true` (or automation `approvalMode:"human"`), the TUI can privately investigate and refresh the review, approve, post human REQUEST_CHANGES feedback, or defer without publishing. Investigation history is durable workflow output and is never posted.

## `pr_review_sweep`

Required: `repo`.

Optional: `approvalMode` = `"human"`; `agent` = `"claude"`; `model` = `""`; `judgeAgent` = `"claude"`; `judgeModel` = `""`; `judgeMaxTurns` = `50`; `judgeMaxBudgetUsd` = `5`; `maxApprovalRevisions` = `2`; `verificationProfile` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `approvalJudgePromptTemplate` = `""`; `approvalJudgePromptTemplateSource` = `""`; `maxNew` = `5`; `maxActive` = `5`.

## `runtime_health` (internal)

Required: none.

Optional: none beyond the shared policy envelope. This read-only operator workflow executes Java, Go, Python, Ruby, Node, npm, and npx probes on both the broad execution worker and the isolated verification worker.
