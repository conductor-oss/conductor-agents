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

Optional: `agent` = `""`; `design` = `false`; `designDir` = `"docs/design"`; `designHumanApproval` = `true`; `designMaxIterations` = `5`; `designMaxTurns` = `250`; `designMaxBudgetUsd` = `50`; `designModel` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `fixPromptTemplate` = `""`; `fixPromptTemplateSource` = `""`; `maxApprovalRevisions` = `2`; `maxBudgetUsd` = `50`; `maxFixAttempts` = `4`; `maxSubtasks` = `4`; `maxTurns` = `250`; `model` = `""`; `openspecPlanAgent` = `""`; `openspecPlanModel` = `""`; `openspecMaxTurns` = `500`; `openspecMaxBudgetUsd` = `50`; `openspecHumanApproval` = `true`; `openspecMaxIterations` = `5`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`.

Addressing review feedback always routes through the full `code_parallel` decompose/fork/merge sub-workflow (the earlier `engine` choice between that and a single `coding_agent` session was removed — `code_parallel` was already the default, and the single-session path added a second, less capable code path with no independent value).

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

Optional: `replyBody` = `""`; `reconcileModel` = `""`; `reconcileMaxResolutionAttempts` = `3`; `reconcileMaxBudgetUsd` = `50.0`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`. The caller must approve and locally verify the candidate first. This workflow performs the one candidate guard, remote-head guard, exact commit push, and success comment -- it completes once the branch is updated and does not wait on or poll CI; GitHub's own PR page reports check status independently.

When the remote-head guard finds the PR branch has moved since the candidate was checked out, it does not simply give up: it merges the remote's new commits in (`reconcile_branch_drift`, agent-resolved on conflict, bounded by `reconcileMaxResolutionAttempts`). A clean merge (the remote's changes never touched the candidate's own changed files) is trusted and pushed without re-verification; a resolved conflict is genuinely new content and is re-verified through `test_cycle` before it is ever trusted for publication (`publicationState: "conflict_verification_failed"` if that re-verification fails). Only resolution failing outright still reports `publicationState: "branch_drift"` with nothing pushed. `reconcileState`/`reconcileTokens`/`reconcileCostUsd` in the output report what happened, if anything.

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

Optional: `callerWorkflow` = `"code_parallel"`; `branchRunId` = `""`; `changeBranch` = `""`; `openspecPlanModel` = `""`; `openspecMaxTurns` = `500`; `openspecMaxBudgetUsd` = `50`; `openspecHumanApproval` = `true`; `openspecMaxIterations` = `5`; `codeModel` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`; `maxFixAttempts` = `4`; `precomputedPlan` = `{}`; `specContextPath` = `""`; `usePrecomputedPlan` = `false`; `design` = `false`; `designDir` = `"docs/design"`; `designHumanApproval` = `true`; `designMaxIterations` = `5`; `designMaxTurns` = `500`; `designMaxBudgetUsd` = `50`; `designModel` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `inPlace` = `false`; `workspacePath` = `""`; `contextPaths` = `[]`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`.

After merging the subtask branches this runs `test_cycle` in targeted mode, so the candidate it hands back has been tested and, where possible, repaired. `maxFixAttempts` bounds that repair loop. The result is diagnostic: `testCycleState` and `testsPassed` are returned for the caller to gate on, and a red suite never withholds the candidate commit. Each subtask also runs its own targeted `test_cycle` with a single fix attempt before its branch is merged. `allowAgentTestPlan`/`allowAgentAuthoredTests` (both default `true` here, unlike `test_cycle`/`test_agent_fallback`'s own default of `false`) are forwarded to the post-merge `test_cycle` call so a targeted verification that cannot map a changed file to an exact test tries the agent-assisted fallback before ending `command_discovery_blocked`.

`callerWorkflow` is forwarded to `design_docs` (when `design:true`) so its human-review checkpoint
correctly identifies the real top-level launcher; a caller that embeds `code_parallel`
(`issue_to_pr`, `address_pr`) passes its own name here instead of the default.

## `code_revision_loop` (internal)

Required: `worktreePath`, `workflowId`, `loopId`, `prompt`, `modelResolution`, `bestCommit`.

Optional: `promptTemplate` = `""`; `promptTemplateSource` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50.0`; `checks` = `[]`; `findings` = `[]`; `accepted` = `false`; `round` = `1`; `maxRounds` = `8`; `plateauCount` = `0`.

## `code_subtask` (internal)

Required: `repoPath`, `name`, `prompt`, `model`.

Optional: `promptTemplate` = `""`; `promptTemplateSource` = `""`; `templateKey` = `"code"`; `promptContext` = `{}`; `maxTurns` = `250`; `maxBudgetUsd` = `50.0`; `specContextPath` = `""`; `contextPaths` = `[]`; `allowedTools` = `[]`; `allowedWriteRoots` = `[]`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`.

`allowAgentTestPlan`/`allowAgentAuthoredTests` (both default `true`, unlike `test_cycle`/`test_agent_fallback`'s own default of `false`) are forwarded to this subtask's own single-fix-attempt `test_cycle` call, same rationale as `code_parallel` above.

## `dag_plan_approval` (internal)

Required: `repoPath`, `instruction`, `callerWorkflow`.

Optional: `contextPaths` = `[]`; `maxTasks` = `25`; `planAgent` = `""`; `planModel` = `""`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `planMaxTurns` = `500`; `planMaxBudgetUsd` = `50`; `planMaxIterations` = `5`.

Author and get human approval for a dependency-DAG implementation plan (id/description/dependsOn/
files/acceptanceCriteria/checks per task), extracted from `feature_campaign`'s own `plan_loop` so
it can be reused -- unlike `design_loop`, there was no existing second implementation to
reconcile with (`code_parallel`'s own "plan_loop" is a fully-automatic validator-repair loop with
no human involved, a different concept entirely), so this is a standalone extraction that
faithfully preserves `plan_loop`'s prior behavior: adjustable per-iteration turn/budget via the
same checkpoint-decision shape `feature_campaign`'s other checkpoints use, and
`continue`/`revise`/`adopt_edits`/`stop` actions. Never hard-fails: an unapproved plan resolves
with `approved:false` (and `stopped` distinguishing an explicit stop from exhausting
`planMaxIterations`) for the caller to act on. `callerWorkflow` must be the caller's own
top-level workflow name, matching `design_docs`'/`pr_draft_approval`'s same requirement.
`allowedWriteRoots` (the approved plan's own declared file scope) is surfaced in output since a
sub-workflow's internal task refs -- unlike a plain DO_WHILE loop's -- aren't reachable from the
caller once the loop ends.

## `design_docs` (internal)

Required: `instruction`, `repoPath`, `callerWorkflow`.

Optional: `designAgent` = `""`; `designDir` = `"docs/design"`; `designMaxBudgetUsd` = `50`; `designMaxIterations` = `5`; `designMaxTurns` = `500`; `designModel` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `humanApproval` = `true`; `failClosed` = `true`; `contextPaths` = `[]`.

Shared by every caller that needs a design phase: `code_parallel` (and so `issue_to_pr` and
`address_pr`, both of which embed `code_parallel`) and `feature_campaign` both delegate their
design phase here instead of each maintaining their own loop. `callerWorkflow` must be the
caller's own top-level workflow name -- required, no default, since design_docs is reachable up
to two levels deep (`issue_to_pr` -> `code_parallel` -> `design_docs`) and the TUI's approval
dispatch needs the real top-level name, not an intermediate sub-workflow's. `humanApproval` picks
a human WAIT checkpoint (which may also request a bounded turn/budget
increase, or explicitly stop early, via the same checkpoint-decision shape `feature_campaign`'s
own checkpoints use) or an automated agent design-judge session. `failClosed` (default `true`,
matching every existing caller's prior behavior) TERMINATEs the whole workflow `FAILED` when a
design is never approved; `feature_campaign` passes `false` so an unapproved design degrades its
own campaign outcome instead of failing the entire run.

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

Required: none.

Optional: `repo` = `""`; `repoPath` = `""`; `workspacePath` = `""`; `instruction` = `""`; `issueNumber` = `0`; `keepWorktree` = `true`; `changeBranch` = `""`; `inPlace` = `false`; `contextPaths` = `[]`; `createPr` = `false`; `requirePrApproval` = `false`; `maxApprovalRevisions` = `2`; `prBase` = `"main"`; `prTitle` = `""`; `prBody` = `""`; `prDraft` = `false`; `designDir` = `"docs/design"`; `designAgent` = `"claude"`; `designHumanApproval` = `true`; `designModel` = `""`; `planAgent` = `"claude"`; `planModel` = `""`; `codeAgent` = `"claude"`; `codeModel` = `""`; `reviewAgent` = `"claude"`; `reviewModel` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`; `maxFixAttempts` = `4`; `maxTasks` = `25`; `maxParallelism` = `6`; `maxWaves` = `20`; `designMaxRevisions` = `5`; `planMaxRevisions` = `5`; `finalMaxRevisions` = `3`; `useImportedPlan` = `false`; `importedPlan` = `{}`; `importedDesignLocation` = `""`; `specContextPath` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `revisionPromptTemplate` = `""`; `revisionPromptTemplateSource` = `""`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`.

`allowAgentTestPlan`/`allowAgentAuthoredTests` (both default `true`, unlike `test_cycle`/`test_agent_fallback`'s own default of `false`) are forwarded to the final full-suite `test_cycle` sub-workflow, same rationale as `code_parallel`/`address_pr` above.

The design phase itself is now the shared `design_docs` sub-workflow (also used by
`code_parallel`/`issue_to_pr`) rather than a separate inline implementation. `designHumanApproval`
(default `true`, matching prior behavior) picks a human WAIT checkpoint or an automated
agent design-judge session; a design that's never approved degrades the campaign's own
`outcome` to `incomplete` rather than failing the whole run (`design_docs`'s `failClosed:false`).

The final verification phase itself is now the standalone `final_verification` sub-workflow
(no other workflow needs an analogous human-reviewed final-verification checkpoint, so this is
extraction-only, not a merge). It always runs when the campaign's own `outcome` is still `running`
by the time the final phase is reached -- an earlier phase's explicit stop is never silently
overridden. `finalMaxRevisions` (default `3`, matching prior behavior) bounds its review/checkpoint
rounds before an unresolved run resolves `incomplete`.

`requirePrApproval` (only meaningful when `createPr` is also true) gates the verified candidate's
PR draft behind `pr_draft_approval` -- the same human-approval-with-bounded-revisions sub-workflow
`issue_to_pr`'s `approvePr` uses -- before it's ever pushed. `false` (the default) publishes
automatically, matching prior behavior. `maxApprovalRevisions` (default `2`, only meaningful
alongside `requirePrApproval`) bounds how many human-requested revision rounds `pr_draft_approval`
will run before a still-pending approval is relabeled `revision_exhausted`.

Two independent choices, each "at least one of," both runtime-enforced by `campaign_defaults`/
`campaign_workspace` (`workspace_prepare`) rejecting the run at start if unmet -- neither is
schema-required because either alone is legitimate:

- **What to build:** `instruction` (free text) or `issueNumber` (a GitHub issue number, requires
  `repo`). When `issueNumber` is set, the campaign fetches that issue's title/body -- plus any
  issue/PR/doc/CI links the body itself references, resolved the same untrusted-evidence way
  `pr_review` resolves links in PR feedback -- and uses that as the effective instruction instead.
  The originating issue is surfaced in output (`issueNumber`/`issueTitle`/`issueUrl`), and when
  `createPr` is also true the PR body gets a trailing `Closes #<n>`.
- **Where to work:** `repo` (a git URL or `owner/name` slug, resolved the same way
  `pr_review`/`address_pr`/`issue_to_pr` resolve it) clones into a fresh temp workspace and works
  from there; `repoPath` uses an existing local checkout directly; `workspacePath` inherits an
  already-prepared worktree (e.g. from a parent campaign). Passing a git URL as `repoPath` fails
  fast with a clear error instead of being treated as a local path -- use `repo` for that case.

These two choices are orthogonal: an issue can drive implementation against either a freshly
cloned `repo` or an existing local `repoPath` checkout, and a plain `instruction` works the same
way against either source.

`prBody` is a summary source, not a free-form PR template. Publication normalizes it to one
`## Summary` paragraph; when empty, the effective instruction (from `instruction` or the fetched
issue) supplies the summary.

## `github_demo`

Required: `repoUrl`, `instruction`.

Optional: `changeBranch` = `"conductor-harness-change"`; `base` = `""`; `agent` = `"claude"`; `model` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `prTitle` = `""`; `maxTurns` = `300`; `maxBudgetUsd` = `50.0`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`.

`allowAgentTestPlan`/`allowAgentAuthoredTests` (both default `true`, unlike `test_cycle`/`test_agent_fallback`'s own default of `false`) are forwarded to the full-suite `verify` `test_cycle` call, same rationale as `code_parallel`/`address_pr` above.

## `implementation_waves` (internal)

Required: `repoPath`, `callerWorkflow`, `plan`, `remainingTaskIds`.

Optional: `allowedWriteRoots` = `[]`; `codeAgent` = `""`; `codeModel` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `reviewAgent` = `""`; `reviewModel` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `revisionPromptTemplate` = `""`; `revisionPromptTemplateSource` = `""`; `maxTasks` = `25`; `maxParallelism` = `6`; `maxWaves` = `20`; `implementationMaxTurns` = `500`; `implementationMaxBudgetUsd` = `50`; `specContextPath` = `""`; `contextPaths` = `[]`.

Schedule and implement an approved dependency-DAG plan in dependency-ordered, file-disjoint
parallel waves, extracted from `feature_campaign`'s own `implementation_loop` so it can be
reused -- like `dag_plan_approval`, this is a standalone extraction: `code_parallel`'s own
parallel-implementation mechanism is a single non-staged fan-out/fan-in with no per-wave human
checkpoint, a different concept entirely, so there's nothing to merge with. Each wave forks one
`campaign_subtask` per ready task, joins, integrates, reviews, and pauses at a human checkpoint
(`continue`/`revise`/`adopt_edits`/`stop`, with the same adjustable turn/budget mechanism
`design_docs`/`dag_plan_approval` use). Never hard-fails: resolves with `outcome` `"running"`
(plan fully implemented, or `maxWaves` reached) or `"incomplete"` (a human explicitly stopped).
`callerWorkflow` must be the caller's own top-level workflow name, matching
`design_docs`'/`dag_plan_approval`'s same requirement.

## `final_verification` (internal)

Required: `repoPath`, `callerWorkflow`, `instruction`.

Optional: `branch` = `""`; `allowedWriteRoots` = `[]`; `reviewAgent` = `""`; `reviewModel` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `finalMaxIterations` = `3`; `finalMaxTurns` = `500`; `finalMaxBudgetUsd` = `50`; `specContextPath` = `""`; `contextPaths` = `[]`.

A read-only-by-default review agent (with authority to reconcile direct worktree edits and fix
actionable checkpoint feedback) verifies the campaign against its instruction, then a human
reviews via the `final_checkpoint` WAIT task (`continue`/`revise`/`adopt_edits`/`stop`, with the
same adjustable turn/budget mechanism `design_docs`/`dag_plan_approval`/`implementation_waves`
use), extracted from `feature_campaign`'s own `final_loop` so it can be reused -- neither
`code_parallel` nor `issue_to_pr` has an analogous human-reviewed final-verification checkpoint,
so this is a standalone extraction, not a merge. Never hard-fails: resolves `outcome` `"verified"`
(explicit continue, committed) or `"incomplete"` (an explicit stop, or `finalMaxIterations`
reached without either). `callerWorkflow` must be the caller's own top-level workflow name,
matching `design_docs`'/`dag_plan_approval`'s/`implementation_waves`'s same requirement.

## `issue_resolution_sweep`

Required: `repo`.

Optional: `issueLabel` = `"conductor:auto"`; `approvalMode` = `"human"`; `agent` = `"claude"`; `model` = `""`; `judgeAgent` = `"claude"`; `judgeModel` = `""`; `judgeMaxTurns` = `50`; `judgeMaxBudgetUsd` = `5`; `maxApprovalRevisions` = `2`; `verificationProfile` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `designJudgePromptTemplate` = `""`; `designJudgePromptTemplateSource` = `""`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `approvalJudgePromptTemplate` = `""`; `approvalJudgePromptTemplateSource` = `""`; `maxNew` = `1`; `maxActive` = `1`.

## `issue_to_pr`

Required: `repo`, `issueNumber`.

Optional: `base` = `"main"`; `approvePr` = `false`; `design` = `false`; `designDir` = `"docs/design"`; `designHumanApproval` = `true`; `designMaxIterations` = `5`; `designMaxTurns` = `500`; `designMaxBudgetUsd` = `50`; `designModel` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `maxApprovalRevisions` = `2`; `openspecHumanApproval` = `true`; `openspecMaxIterations` = `5`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `maxTurns` = `300`; `maxBudgetUsd` = `50`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`.

`allowAgentTestPlan` and `allowAgentAuthoredTests` forward to the final full-suite `test_cycle` sub-workflow exactly as documented under `test_cycle` above; both now default `true` here too (matching `address_pr`/`code_parallel`/`feature_campaign`/`github_demo`), since leaving a change permanently blocked on missing test-file mapping is worse than trying the agent fallback first; the resulting `agentAuthoredTest` forces `prDraft` to `true` as defense in depth even when delivery otherwise passed.

`approvePr` was previously declared but never read (the gate was hardcoded to skip it) -- now wired for real: `true` routes the verified candidate's not-yet-published PR draft through `pr_draft_approval` (see below) before it's ever pushed, `false` (the default) auto-approves exactly as before. The TUI defaults it to `true` for interactive chat-launched runs.

## `pr_draft_approval` (internal)

Required: `callerWorkflow`, `repoPath`, `workspacePath`, `branch`, `title`, `body`, `candidateCommit`.

Optional: `summary` = `""`; `base` = `"main"`; `issueNumber` = `0`; `subtasks` = `[]`; `verificationState` = `"passed"`; `verification` = `{}`; `merged` = `[]`; `conflicts` = `[]`; `totalTokens` = `0`; `totalCostUsd` = `0`; `originalContext` = `""`; `maxApprovalRevisions` = `2`; `agent` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50`; `maxSubtasks` = `4`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `openspecMaxIterations` = `5`.

`issue_to_pr`'s human-approval gate for a not-yet-published PR draft (title/body/base/head,
no PR number yet), extracted to its own sub-workflow so any workflow that verifies a candidate
first and only wants to publish once a human is satisfied can reuse it (`feature_campaign` also
uses it, via its own `requirePrApproval` flag -- see below). A human must explicitly approve the
draft via the `pr_gate` WAIT task, optionally requesting up to `maxApprovalRevisions` bounded,
independently re-verified revisions (via `code_parallel`) first, resolving to exactly one of:
`approved`, `suppressed`, `blocked`, `verification_blocked`, `revision_exhausted`.
`callerWorkflow` must be the caller's own top-level workflow name (e.g. `"issue_to_pr"`,
`"feature_campaign"`) -- the TUI's approval dispatch branches on it, and without it a gate living
in this sub-workflow would report its own name instead of the caller's.

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
