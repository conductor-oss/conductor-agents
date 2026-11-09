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

**A `repoPath` does not have to be a git repository yet.** Point any workflow at an existing
directory — empty, or holding files that were never committed — and the first step that touches it
makes it a repository: `git init`, a local identity when none is configured, and an empty base commit
that leaves the directory's own files exactly as they were (uncommitted). The agent worker does this
before it reads `HEAD` (`coding_agent` output `initializedRepo`), so a program's planner or an
intake classifier never fails on a fresh directory; `cc_program`'s first step (`repo_memory_read`) and
the workspace steps (`workspace_prepare`, `prepare_repo`) do the same. A directory inside an existing
repository belongs to that repository and is left alone, and a path that does not exist is refused,
never created. Before this, run `edc1c893` failed four times on `git rev-parse HEAD` in an empty
directory and ended before planning.

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

`address_pr`'s human-approval gate for one already-verified candidate, extracted to its own sub-workflow to keep `address_pr`'s own graph small. A verified candidate is never auto-published: the `address_gate` `WAIT` task always pauses for an explicit human `approve`/`revise`/`stop` decision, offering up to `maxApprovalRevisions` bounded, independently re-verified revision rounds first (each `revise` reruns `code_parallel`, marked `optional: true` so an infra failure there degrades to `verification_blocked` rather than failing the whole `address_pr` run). Resolves `approvalState` to exactly one of: `approved`, `suppressed`, `verification_blocked`, `revision_exhausted` (a still-`pending` state when `maxApprovalRevisions` is exhausted). A decision the gate does not recognise (anything but `approve`/`revise`/`stop`, or a `revise` with no feedback) never ends the run: the state stays `pending`, the loop re-opens `address_gate` with `draft.unrecognizedDecision` naming what arrived, and that round counts against `maxApprovalRevisions`. `address_pr`'s `publication_gate` only publishes when this resolves to `approved`.

## `test_agent_fallback` (internal)

Required: `repoPath`, `candidateCommit`, `discoveryOutcome`.

Optional: `testMode` = `"targeted"`; `allowHeavySuites` = `false`; `includeBrowserTests` = `false`; `allowAgentTestPlan` = `false`; `allowAgentAuthoredTests` = `false`; `discoveryReason` = `""`; `changedPaths` = `[]`; `repairRoots` = `[]`; `agent` = `""`; `model` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50`.

`test_cycle`'s agent-assisted discovery/authoring fallback, extracted to its own sub-workflow to keep `test_cycle`'s own graph small. A no-op that echoes `candidateCommit`/`repairRoots` straight back unless `allowAgentTestPlan` is `true` and `discoveryOutcome` is `configuration_blocked`. Tier 1: a read-only coding agent's proposed test plan, validated against this module's strictest gate (`validate_agent_argv`: the entrypoint must belong to a build system the repository actually shows evidence of, every path-like token must resolve to a real file, every claimed covered path must be a real changed path, and every changed file that looks like a test must be covered by some proposal). Tier 2, only reached when tier 1 also finds nothing and `allowAgentAuthoredTests` is `true`: a coding agent (pinned to the Claude backend, tools limited to `Read`/`Grep`/`Glob`/`Write`/`Edit` — no `Bash`) may author exactly one new test file, discovering the repository's own test convention itself rather than following any hardcoded per-language rule. The new file is never trusted on the agent's say-so: it must be the single new, test-shaped file (nothing else touched), must textually reference the changed code, and must pass a red/green check — failing when run against the candidate's pre-change baseline and passing against the real candidate change — before it is committed and treated as the resolved test plan. `agentAuthoredTest`/`agentAuthoredTestPath` in the output report whether this happened. It owns no branch: an accepted authored test is committed to whatever branch `repoPath` is already on.

## `test_cycle` (internal)

Required: `repoPath`, `candidateCommit`.

Optional: `testMode` = `"targeted"`; `maxFixAttempts` = `4`; `testCommands` = `[]`; `testPlanTemplate` = `""`; `testPlanTemplateSource` = `""`; `allowHeavySuites` = `false`; `includeBrowserTests` = `false`; `allowAgentTestPlan` = `false`; `allowAgentAuthoredTests` = `false`; `priorVerification` = `{}`; `allowedWriteRoots` = `[]`; `agent` = `""`; `model` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `50`; `repairPromptTemplate` = `""`; `repairPromptTemplateSource` = `""`; `sandboxChecks` = `false`.

Runs the repository's tests for one exact candidate commit and fixes what fails. `testMode` = `"targeted"` derives an exact-test or affected-unit plan from the candidate's diff; `testMode` = `"full"` runs the repository-wide suite and is the mode to use before publishing a PR — full mode reruns the whole suite on every repair iteration, so reserve it for a final pre-publication gate, not a review-feedback loop. `allowHeavySuites` is honoured only in full mode and is what permits an integration or end-to-end target; it is never inferred. `includeBrowserTests` is also full-mode-only and adds the repository's dedicated Playwright/Cypress script when one exists — it never triggers a browser install: if binaries are not already provisioned on the host, this reports a diagnostic in `rejectedCandidates` rather than blocking the run. `testCommands` and `testPlanTemplate` are the two forms of the operator override and both outrank every other source: `testCommands` is a structured argv list; `testPlanTemplate` is the same shape as an inline JSON string, for a caller that only has text to pass, and is rejected outright if it starts with `@` (the templating convention for resolving from a file in the checkout, which the candidate controls) rather than falling back to it. When both are supplied, `testCommands` wins, since it cannot suffer a JSON-parsing surprise. Below the operator override, the precedence is repository guides, then `.conductor-code/verification.json`, then build-system inference, and — only when every one of those comes up empty — `test_agent_fallback` (see above), gated on `allowAgentTestPlan`/`allowAgentAuthoredTests`. The agent is invoked at most once per candidate SHA and is never re-proposed after a repair; its accepted plan is carried forward the same way a `priorVerification` obligation is. Repair writes are restricted to `allowedWriteRoots` — handed to `test_discover`, whose `repairScope` the repair agent runs under — falling back to the discovered changed-scope directories only when the caller declares none. The declared scope was received and never forwarded: a fork's repair agent scoped to the whole `store/` directory implemented sibling items' files to make the item's own acceptance test pass, and those commits collided with the owners' forks at integration (run `a33d7061`).

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

Optional: `approvalMode` = `"human"`; `agent` = `"claude"`; `model` = `""`; `judgeAgent` = `"claude"`; `judgeModel` = `""`; `judgeMaxTurns` = `50`; `judgeMaxBudgetUsd` = `5`; `maxApprovalRevisions` = `2`; `verificationProfile` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `fixPromptTemplate` = `""`; `fixPromptTemplateSource` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `designJudgePromptTemplate` = `""`; `designJudgePromptTemplateSource` = `""`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `approvalJudgePromptTemplate` = `""`; `approvalJudgePromptTemplateSource` = `""`.

## `automation_reset` (internal)

Required: `repo`, `kind`, `number`, `revision`. Optional: none.

## `campaign_subtask` (internal)

Required: `repoPath`, `task`, `wave`.

Optional: `agent` = `"claude"`; `model` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`; `resumeSessionId` = `""`; `feedback` = `""`; `specContextPath` = `""`; `contextPaths` = `[]`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`.

## `code_parallel`

Required: `repoPath`, `instruction`.

Optional: `callerWorkflow` = `"code_parallel"`; `branchRunId` = `""`; `changeBranch` = `""`; `openspecPlanModel` = `""`; `openspecMaxTurns` = `500`; `openspecMaxBudgetUsd` = `50`; `openspecHumanApproval` = `true`; `openspecMaxIterations` = `5`; `codeModel` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`; `maxFixAttempts` = `4`; `precomputedPlan` = `{}`; `specContextPath` = `""`; `usePrecomputedPlan` = `false`; `design` = `false`; `designDir` = `"docs/design"`; `designHumanApproval` = `true`; `designMaxIterations` = `5`; `designMaxTurns` = `500`; `designMaxBudgetUsd` = `50`; `designModel` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `inPlace` = `false`; `workspacePath` = `""`; `contextPaths` = `[]`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`; `singleSubtask` = `false`.

After merging the subtask branches this runs `test_cycle` in targeted mode, so the candidate it hands back has been tested and, where possible, repaired. `maxFixAttempts` bounds that repair loop. The result is diagnostic: `testCycleState` and `testsPassed` are returned for the caller to gate on, and a red suite never withholds the candidate commit. Each subtask also runs its own targeted `test_cycle` with a single fix attempt before its branch is merged. `allowAgentTestPlan`/`allowAgentAuthoredTests` (both default `true` here, unlike `test_cycle`/`test_agent_fallback`'s own default of `false`) are forwarded to the post-merge `test_cycle` call so a targeted verification that cannot map a changed file to an exact test tries the agent-assisted fallback before ending `command_discovery_blocked`.

`callerWorkflow` is forwarded to `design_docs` (when `design:true`) so its human-review checkpoint
correctly identifies the real top-level launcher; a caller that embeds `code_parallel`
(`issue_to_pr`, `address_pr`) passes its own name here instead of the default.


**Testing after the merge.** The merged change is tested targeted when its files map to exact tests. When they do not and the repository has a whole-suite command, documented or inferred, `test_mode_select` runs that suite instead, and forks skip the agents that would look for or write a test of their own. The output separates delivery from proof: `verified` means planned, merged and delivered; `deliveredAndTested` also requires its tests to have passed, or the change to need none; `verdict` is `DELIVERED`, `DELIVERED_UNTESTED`, `TESTS_FAILED` or `INCOMPLETE`. The `single` and `parallel` routes of `sdlc_intake` report `delivered` from `deliveredAndTested`. `singleSubtask` folds the validated plan into one work item built by one agent; the `single` route sets it.

## `code_subtask` (internal)

Required: `repoPath`, `name`, `prompt`, `model`.

Optional: `allowedTools` = `[]`; `allowedWriteRoots` = `[]`; `acceptanceTests` = `[]`; `maxBudgetUsd` = `50`; `maxTurns` = `250`; `promptContext` = `{}`; `promptTemplate` = `""`; `promptTemplateSource` = `""`; `contextPaths` = `[]`; `specContextPath` = `""`; `templateKey` = `"code"`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`; `feedbackCorrelationId` = `""`; `feedbackItem` = `""`; `feedbackSince` = `0`; `feedbackEveryTurns` = `1`; `checkpointEveryTurns` = `0`; `testCommands` = `[]`; `outputSchema` = `{}`; `sandboxChecks` = `false`; `modelTier` = `0`.

`allowAgentTestPlan`/`allowAgentAuthoredTests` (both default `true`, unlike `test_cycle`/`test_agent_fallback`'s own default of `false`) are forwarded to this subtask's own single-fix-attempt `test_cycle` call, same rationale as `code_parallel` above. `allowedWriteRoots` is both the write guard and the delivery audit's planned-paths set, minus `acceptanceTests`: the item's frozen acceptance tests are inside the write roots only so a wrong assertion can be amended, so the audit (`delivery_audit` and `code_subtask_delivery_outcome`, `optionalPaths`) never counts one left untouched as a missing delivery — every item with a frozen test was audited `incomplete_delivery` and ran three no-op repair agents per fork (run `a33d7061`, 15 of 16 forks). Nor is a planned path that is already in HEAD's tree and left unchanged (`presentPaths`): a review re-dispatch changes only what the reviewer asked for, and the item's other files were delivered by its earlier attempt (store-hashes ran three no-op repair agents for the `hashes.go` it had delivered a wave earlier, run `ec0fb6dc`). The audit lists every untracked file by itself, so the first files of a new directory are audited as files, not as one collapsed `resp/` entry that made every planned file "missing" (same run). An implementer that reports `pushback` instead of a change gets audit state `pushback` and no repair agent (the Lead decides the pushback at integration; a repair agent rewrote `reader.go` after resp-reader's pushback and the Lead then discarded that fork); the outcome state is `pushback`, never `passed`. Likewise an implementer that made no change because the shared branch already satisfies its item (a sibling delivered the work, usually inside the sibling's own file) reports `alreadySatisfied` with its evidence: audit and outcome state `satisfied`, no repair agent (six repair agents re-verified cmd-dispatch's prose claim, created nothing, and the item blocked as an empty delivery, run `90e5e3aa`); the team's peer reviewer decides the claim at integration. A claim made alongside actual changes is an ordinary delivery. The fork's `commit` stages only changes inside `allowedWriteRoots` plus `acceptanceTests`: the agent runner's guard refuses Write/Edit outside the roots, but Bash can write any file of the worktree (sv-cmd-decr removed a sibling's type declaration through Bash after the Edit tool refused it, run `ba2491ff`), so a change outside the roots is left unstaged and reported as `outsideScope` — merged into the fork's `deniedWrites`, the Lead's scope-widening evidence — never integrated as an edit nobody owns. `test_cycle`'s repair commit is confined to its `allowedWriteRoots` the same way.

## `convergence_spike`

Required: `repoPath`.

Optional: `subtasks` = `[]`; `contractPaths` = `[]`; `ceilingUsd` = `20`; `warnFraction` = `0.8`; `maxWaves` = `8`; `maxAttempts` = `2`; `codeModel` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `10`; `maxFixAttempts` = `2`; `branchRunId` = `""`; `inPlace` = `false`; `workspacePath` = `""`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`.

The roadmap Phase 1 H1/H4 convergence spike. `subtasks` is the pre-registered OVERLAPPING plan (`[{id, description, files}]`, files deliberately shared across items); `contractPaths` names the frozen contract the forks build against but must not edit. Dispatch runs in ownership-gated **waves** (`maxWaves` caps the loop): each wave dispatches only the items whose shared files are free — two agents never hold the same file concurrently — then `convergence_integrate` rebases that wave onto the moving shared head (never a big-bang merge), releases the wave's file ownership, and records each agent's real spend. A fork that delivers no commit or hits a rebase conflict is **re-queued for a later wave** (up to `maxAttempts` per item — the moving head is what lets a retry land); only a frozen-contract edit or an exhausted retry budget is a terminal reopen. `ceilingUsd` is the aggregate spend ceiling: the reused `budget_gate_check` enforces the not-startable rule (a swarm-mode run with `ceilingUsd <= 0` refuses to start, roadmap H5) and, once per-wave spend is recorded, **halts dispatch of further waves** when accumulated spend reaches the ceiling. The workflow emits an honest GO/NO-GO `verdict` and `report`; a NO-GO names the precise mechanism (rebase conflict, contract controlled-event, budget-halt undispatched items, or a non-green shared branch).

## `team_build`

Required: `repoPath`.

Optional: `subtasks` = `[]`; `contractPaths` = `[]`; `instruction` = `""`; `forceSingle` = `false`; `forceTeam` = `false`; `enablePeerReview` = `true`; `ceilingUsd` = `40`; `warnFraction` = `0.8`; `maxWaves` = `12`; `maxWidth` = `6`; `maxAttempts` = `2`; `maxReviewRounds` = `2`; `stallAttempts` = `2`; `qualityChanges` = `2`; `maxReviewAttempts` = `2`; `maxContractEvents` = `2`; `planUsage` = `{}`; `feedbackEveryTurns` = `1`; `checkpointEveryTurns` = `10`; `bestOf` = `1`; `authorAcceptanceTests` = `true`; `acceptanceTestPattern` = `"tests/acceptance/test_accept_{id}.py"`; `parentCorrelationId` = `""`; `subsystemId` = `""`; `maxHumanConstraints` = `3`; `sandboxChecks` = `true`; `codeModel` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `10`; `reviewMaxTurns` = `60`; `reviewMaxBudgetUsd` = `3`; `maxFixAttempts` = `2`; `branchName` = `"team-build"`; `branchRunId` = `""`; `inPlace` = `false`; `workspacePath` = `""`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`; `maxRescues` = `2`; `baseModelTier` = `0`.

The roadmap Phase 2 team cycle (with the Phase 3 monitor wired in): each wave a `team_monitor` watches the board with bounded authority — it nudges/re-prioritizes a thrashing item (`stallAttempts`) or a repeatedly-rejected one (`qualityChanges`) autonomously, and escalates anything bigger (a blocked item, duplication, a budget breach) to the exception queue for a human to drive from the web Swarm Console (`make console`). The roadmap Phase 2 team cycle: a collaborating agent team builds a multi-item feature on one shared branch. `subtasks` is the decomposed work (`[{id, title, description, kind, dependsOn, files, acceptanceCriteria, priority}]`); `contractPaths` names the frozen contract (H4). `team_route` first reads the mode off the decomposition (H9/H12): one small piece (or `forceSingle`) routes to plain `code_parallel` with **zero board overhead**; multi-item/coupled work (or `forceTeam`) runs the board cycle. The cycle is a `DO_WHILE` of **standup → fan out → implement ∥ review → integrate → learn → re-plan** over a mutable tracking board: each wave the Lead sizes the team from dependency-ready work (`maxWidth` caps it, throttled as spend nears the ceiling), gates dispatch on shared-file ownership (H1), and injects the live board rollup + teammates' lessons + the item's directives into each implementer. `team_integrate` rebases the wave onto the moving shared head (never a big-bang merge), records real spend, and moves clean items to peer review; a different agent reviews each item against its acceptance criteria (H8/H13) and `changes` reopens it with the findings attached (bounded by `maxReviewRounds`), `lgtm` marks it done and shares a lesson. `ceilingUsd` enforces the H5 not-startable rule and halts further waves on accumulated spend; `maxWaves` caps the loop; `maxAttempts` bounds transient (empty/conflict) retries. The workflow emits an honest `verdict` (DELIVERED / INCOMPLETE) and `report`; INCOMPLETE names the precise mechanism (blocked items, a budget halt leaving work undone, or a non-green shared branch).

Convergence and honesty rules (closure plan WP1): every item in review is reviewed; `maxReviewRounds` is how many `changes` verdicts an item may absorb, and the next one **quarantines** it (`blocked`, `review_rounds_exhausted`, exception `review_cap`) — a rejected item is never accepted silently; a reviewer that returns no verdict is `unavailable` (re-reviewed next wave, `blocked` after `maxReviewAttempts`). A fork that edits the frozen contract is a **controlled event**: it is never merged, its proposer is `blocked` with `contract_change_pending`, and the Lead rejects the edit at once (a frozen contract stays frozen; the next standup re-dispatches the proposer against the contract as-is, with that constraint); more than `maxContractEvents` proposals from one item block it as `contract_churn`. **Humans are passive observers of the swarm**: they watch, give feedback, and can terminate; nothing waits for their approval. Each wave ends with a liveness check: when no work can move, the run exits at once naming `dependency_starved` or the open escalations, instead of spinning empty waves to `maxWaves`. Before any implementer is dispatched (closure plan WP6, H13), every item with `acceptanceCriteria` gets its acceptance tests written by a **different** agent (`authorAcceptanceTests`, at `acceptanceTestPattern`); they are committed, run raw at the base, and only tests that are **red** before the feature exists are kept — a test that already passes is tautological and is discarded with a lesson. Kept tests join the frozen contract (an implementer's edit of its own oracle is a controlled event, never merged), pin that item's own test gate, and are checked byte-for-byte at the end: a delivered head where a frozen acceptance test was modified is never `DELIVERED`. Steering is identified and has a lifecycle (closure plan WP7, H16): a directive's authority is derived from its author — only a registered actor (`$SWARM_ACTORS_FILE`) writing as `human:<email>` ranks as a human — and a constraint can be lifted, replaced, or lapse after `expiresAfterWaves`; the monitor escalates an item carrying `maxHumanConstraints` or more human constraints, or failing while it carries any (`over_constrained`). An implementer that judges a constraint harmful or impossible reports `pushback` instead of silently obeying or overriding it: the Lead exempts that item from that one directive (it stays in force for everyone else), records the decision as a resolved `pushback` escalation, and re-dispatches the item (bounded by `maxAttempts`, after which it blocks as `constraint_pushback`). Implementers also report lessons, shared as `opinion` (never above medium confidence) until an outcome verifies them; a reviewer can `retract` a lesson its diff proves wrong, and a lesson whose `path` or `path::symbol` ref no longer resolves on the shared branch is dropped from every prompt (closure plan WP8, H10/H15). Every check command — the acceptance red run, each item's test gate, and the final suite — runs OS-sandboxed when `sandboxChecks` is on (the default here): writes confined to the disposable checkout and caches, no network but loopback (closure plan WP9, H17). With `bestOf` > 1 each dispatched item runs that many independent candidate forks under one ownership claim; the integrator keeps the best (delivered and contract-respecting, then subtask tests green, delivery audit passed, fewest files, smallest diff, lowest cost), integrates only it, and discards the rest (H12a: best-of-N inside the swarm). Every agent's spend — implementers, reviewers, the planner (`planUsage`), and final test-repair agents — is recorded on the run budget, and `spendByRole` reports it. Each implementer takes feedback **while it works** (closure plan WP2, H11): a directive posted to its item, `agent:<item>`, or `team` after its prompt was built is injected into the running session after its next tool call (`feedbackEveryTurns`, default every call); a `blocking` directive (the console's `stop`) ends the session, and the item is re-opened with the directive instead of integrating partial work. Every `checkpointEveryTurns` tool calls the implementer's worktree is snapshotted into `refs/cc-checkpoints/<branch>` (H17), and a session that starts on a clean worktree at that checkpoint's parent restores it, so a lost worker costs at most that many tool calls. The final green gate runs `test_cycle` in `full` mode: the integrated branch of several items is verified by the repository's whole documented suite, not only the paths the last change touched. Not every item is code: an item's `deliverable` is `code` or `docs` (design documents, specs, ADRs, research notes; inferred as `docs` when every file is a document) — a `docs` item gets a documents-first prompt, no executable acceptance tests, and a reviewer who is its only oracle. While it works each implementer posts a throttled `activity` heartbeat (its current tool and target), so the run's state can be asked at any time (`GET /api/state`, `POST /api/ask` on the Swarm Console). An implementer refused a file outside its write roots (the worktree guard records it as `deniedWrites` on the fork's output and the integrator on the item's `session` fact) tells the Lead the plan sliced the item too narrowly: at the next standup the item's write scope is widened to that file once no open item still owns it (a resolved `write_scope` notice records it, and the reviewer's diff covers the file), the item waits while an open item still owns it (`scopeWaits`), and a frozen-contract path is never widened — resubmitting work-arounds against the review cap is what halted the resp-cache program (run `fc6467b7`, st-lru / `store/store.go`). Several items refused the same file are widened one at a time: the first owns the file for the rest of that standup and the others wait for it to land (four store items were widened to `store/store.go` in one standup and edited it concurrently, run `a062184f`). A team's write authority ends at the directories its items span: a refused path outside them is never widened (`scopeRefusals`, a resolved `write_scope` notice) — the design team's d-store, a documents item, was widened to `store/errors.go` and `store/store.go` and took ownership of code the breakdown then assigned to the store team (run `a33d7061`); writing outside the subsystem is the program's call. Each acceptance-test author is told what its tests may rely on — the item's transitive dependencies, built first — and which sibling items are built concurrently or later and so do not exist when the tests first run on the implementer's fork: st-lists' test called `Set`/`Exists`/`Type` from st-strings and st-keys, dispatched in the same wave, so no item of that wave could pass its own gate alone and the forks' test-cycle repair agents implemented each other's files to force it (run `a33d7061`). A rebase conflict's mechanism names the shared-branch commit that last changed each conflicting file (`touched`), so the other side of the collision is visible at once. A wave's `finalHead` is read after `team_apply_reviews` (its `finalHead` output), not from the integrate step: an approved acceptance-test amendment is merged by apply_reviews, after integration, and the final test cycle's candidate and the outcome's byte-for-byte check must see that head — the server team re-froze its amended test at the merged head and was judged "modified after frozen" against the integrate step's stale head, blocking cmd and integration on a delivered subsystem (run `ec0fb6dc`). Fork worktrees are short-lived: once the wave has decided a fork — integrated, re-queued, or discarded — the integrator removes its worktree and branch (`removedForks`; the work is on the shared branch, a re-dispatch starts a fresh fork from `refs/cc-checkpoints`), and only a fork held for an acceptance-test amendment keeps its worktree until the reviewer decides. Registered worktrees are not free: Claude Code's Bash sandbox adds deny paths for every worktree registered on the repository, and at 171 of them the sandbox profile exceeded the OS exec argument limit — every implementer's every Bash call failed at spawn (E2BIG, 591 calls in run `ec0fb6dc`) and no agent could build or test. For the same reason `workspace_prepare` removes, before creating a run's worktree, the run worktrees (with their nested fork and review worktrees) of runs the Conductor server reports as finished (`prunedRuns`); a run it cannot ask about is kept, and no branch is ever touched — a finished run's result is its `program-<id>`/`team-build-<id>` branch, not its checkout. Implementers run with the full unattended tool surface (`DEFAULT_ALLOWED_TOOLS`: the language build/test/format entrypoints, `make`, file utilities), so a repository's documented verification command is runnable from inside a fork. A frozen acceptance test is proven on its item's fork alone, so after each wave's merges the integrator re-runs every integrated item's frozen tests at the integrated head (the **integration oracle**, `oracle` on `team_integrate`: `{state, checked, red}`; sandboxed under `sandboxChecks`): an item whose test is red there is re-opened (`integration_oracle_red`, bounded by `maxAttempts`) with the failing output as its directive — its re-dispatch fixes the code, or amends an assertion a sibling's landing falsified, which the peer reviewer decides as any amendment — and a fork held for an amendment is not checked (its test is red at the head by construction). The final `test_cycle` carries the frozen tests' commands as its `priorVerification` obligation next to the repository's own command, and `team_outcome` reads the commands that cycle actually ran (`testCommandsRan`): a head with a frozen test no executed command names is `acceptanceUnproven`, never `DELIVERED`. Acceptance-test authors are told never to assert a sibling's behaviour is absent. Without all three, sv-core's frozen test — "PING is an unknown command", true on its fork — was red at the integrated head once sv-connection registered PING, the final cycle ran only `make test` (which never executes the Python acceptance suite), and the program delivered 6/6 with 2 of its 290 frozen tests failing (run `299b4907`). The final `test_cycle` runs on its **own fork** of the shared branch (`tests_fork`, a `worktree_add` named `tests`; it verifies that fork's head), never in the shared worktree: the agent runner resets the worktree a session ran in to the head it started from, so the store team's final-cycle repair agent, working in the shared worktree while the resp team was integrating, parented its repair commit on a commit before the resp team's r-reader merge (the program branch lost that merge) and left an untracked `internal/resp/writer.go` that aborted r-writer's approved-amendment merge — the resp team ended as a FAILED task (run `a4f1ff1f`). `team_outcome` integrates a green repair like any fork (rebase + fast-forward, `repairIntegrated`; `finalHead` is then that head), discards a red one or one that no longer applies (`repairDropped`, never `DELIVERED`), and removes the fork. What that cycle must prove and may repair comes from the board at the end of the loop (`team_apply_reviews` → `capture_round`): the frozen-test commands of the items that are **done** (`acceptanceCommands`; an undone item's test is red by construction, and the same run's store cycle ran st-core's test and sent its repair agent to implement st-core with write roots derived from the resp team's last commit) and the files the team's items span (`writeRoots` → the cycle's `allowedWriteRoots`). A merge failure on an approved amendment no longer fails the team: the amendment is recorded as not landed and the item rebuilt against the current head. Two more rules from the same run: the Lead widens a refused file at once when every open item that owns it depends (transitively) on the waiting item — st-stats owned `stats.go` and depended on st-core, which needed a `Stats` stub there, a cycle the team waited on for 14 empty waves to `maxWaves`; and a wave that dispatched nothing because every ready item was held for a file (`scopeWaits`) while nothing is in flight is a stall the monitor ends at once (`starved: scope_wait`, each hold posted as a `scope_wait` escalation) instead of spinning to `maxWaves`. An empty delivery whose implementer reports `alreadySatisfied` (its registry and dispatch already written by a sibling inside the sibling's file) is not retried blind: the integrator posts an empty delivery range at the current head with the claim (`claimedSatisfied`), the item goes to peer review, and the reviewer judges the claim against the criteria on the current tree — `lgtm` is done as-is, `changes` names what is missing or misplaced and re-opens the item as any review would; with peer review disabled it stays an empty delivery. Six repair agents and a block on cmd-dispatch starved the server subsystem for lack of this (run `90e5e3aa`). Acceptance criteria written as one text (a planner's paragraph) are split into criteria, never dropped: that run's breakdown wrote every item's criteria as a paragraph, the board kept `[]`, and no team of the run had a frozen acceptance test. A reviewer that needs a change in a file the implementer may not write reports it under `outOfScope` (`{file, change}`), never as a finding: the Lead records those files as widening evidence at once (the same `session` signal an implementer's refusal leaves) and the next standup widens the item, and a round that asks for nothing else is `scopeOnly` — it re-opens the item with the changes as directives but counts against neither the item's review cap nor the monitor's quality signal. d-store's reviewer named `architecture.md` in round one, the Lead widened only after the implementer's refusal a wave later, round two ("not a fault of this implementer, the Lead needs to widen") spent the item's last round, and a new finding in round three quarantined it — the docs rank blocked and the program ended at 0/6 (run `0709d705`). Reviewers are also told to report every blocking problem they can find in one round. An `lgtm` that carries `outOfScope` is the same scope-only round: the item is not done until that change lands (sv-cmd-registry was marked done on such a verdict, its outOfScope — a type declared twice, once in a sibling's file — was never applied, and the command package stayed unbuildable for the rest of run `ba2491ff`). A pushback whose implementer was refused an edit inside the team's span (`deniedWrites`) is a scope gap too, not a refused constraint: the item is re-opened at the same attempt with a resolved `pushback` notice and widened at the next standup — sv-cmd-del and sv-cmd-decr were told to make their frozen tests pass, found the cause in two siblings' files, were refused there, and both blocked as `constraint_pushback` at the attempt cap, ending the server subsystem (same run). The review cap distinguishes a rejected item from a review that keeps finding things: each round the reviewer lists under `unresolved` the previous round's findings the revision did not address (required in its schema). Over the cap with `unresolved` empty — every prior finding was addressed and this round's findings are new — the Lead accepts the item and carries the new findings as directives to the items that depend on it (else to the team), recording a resolved `review_cap` notice; with a prior finding unaddressed, or no `unresolved` report at all, the item is quarantined as before. The architecture document's reviewer raised three findings (all fixed), one new finding about the fix, then two new findings about that fix: the one rank-0 item was quarantined and the program ended at 0/6 (run `a57bf979`). Each peer reviewer reads its own worktree pinned at the reviewed head (`.cc-worktrees/review-<item>`, detached, removed when the verdicts are applied), never the shared integration worktree: the agent runner resets the worktree a session ran in to the HEAD it started from, which undid a sibling team's concurrent merge there and left that team's files untracked, so its next fast-forward aborted and the team failed (run `a062184f`, resp). Every run ends with `run_retrospective`.

**Help for a struggling agent.** A swarm does not give up on an item its agents are stuck on. Wherever an item used to block — attempts exhausted on an empty delivery, a rebase conflict or a red integration oracle, review rounds exhausted with findings unaddressed, a pushback that could not be resolved, frozen-contract churn, a reviewer that never returned a verdict — the Lead first reopens it with a fresh attempt budget (a resolved `rescue` notice; `rescued` on `team_integrate` and `team_apply_reviews`) and the next standup hands it to a stronger teammate: the next tier of the code role's model policy (`modelTier`, passed through `code_subtask` to every agent; tiers are ordered, the first is the default and the index is clamped to the last), briefed as the help for a stuck item and given the item's full history — every dispatch, delivery outcome, review finding, failure mechanism, pushback, refused write and directive — with the instruction to find the root cause rather than repeat what failed. Review rounds restart for the helper. Up to `maxRescues` helpers, each a tier higher; only then does the item block, naming the mechanism. A reviewer that returned no verdict re-reviews a tier up, and a team its program re-runs starts every agent one tier up (`baseModelTier`). The ladder is: retry with feedback, a stronger teammate with the history, the program re-running the team with its diagnosis, and only then a block.


**Result branch.** `branchName` names the branch a run's work lands on; the run's short id is appended so two runs never share one (`swarm/pg-compatible-database-037e1cbe`). The console fills it from the request (`swarm/<words of the request>`); left at the default the branch is `team-build-<run id>`. The run's checkout under `.cc-worktrees/` holds the branch until a follow-up run continues from it (Continue takes the branch over from a finished run's clean checkout) or the next run on the repository prunes finished runs' checkouts; the branch keeps every commit either way, and `git log`, `git diff` and `git merge` on it work from your own checkout.

## `bug_fix`

Required: `repoPath`.

Optional: `bugReport` = `""`; `reproTestPath` = `"test_bug_repro.py"`; `codeModel` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `10`; `maxFixAttempts` = `2`; `branchName` = `"bug-fix"`; `branchRunId` = `""`; `inPlace` = `false`; `workspacePath` = `""`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`.

The roadmap Phase 4 (G2) reproduction-first bug fix. A `code_subtask` authors a failing regression test at `reproTestPath` (and only that file); the raw `test_discover`/`test_plan_resolve`/`test_run` confirm it is **red** at the parent commit (`bug_repro_gate` — a passing test is `cannot_reproduce`, an honest stop, never a silent fix); a second `code_subtask` fixes the source (forbidden from editing the test); `test_cycle` proves the branch **green**; `bug_outcome` emits `FIXED` only if reproduced ∧ green ∧ the repro test was not tampered with (H13). `bugReport` is the free-text report (or the normalized instruction `sdlc_intake` passes).


**Result branch.** `branchName` names the branch a run's work lands on; the run's short id is appended so two runs never share one (`swarm/pg-compatible-database-037e1cbe`). The console fills it from the request (`swarm/<words of the request>`); left at the default the branch is `bug-fix-<run id>`. The run's checkout under `.cc-worktrees/` holds the branch until a follow-up run continues from it (Continue takes the branch over from a finished run's clean checkout) or the next run on the repository prunes finished runs' checkouts; the branch keeps every commit either way, and `git log`, `git diff` and `git merge` on it work from your own checkout.

## `sdlc_intake`

Required: `repoPath`.

Optional: `request` = `""`; `confidenceThreshold` = `0.5`; `forceSingle` = `false`; `forceTeam` = `false`; `ceilingUsd` = `40`; `maxWidth` = `6`; `reproTestPath` = `"test_bug_repro.py"`; `maxReplan` = `2`; `bestOf` = `1`; `codeModel` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `10`; `maxFixAttempts` = `2`; `branchName` = `""`; `branchRunId` = `""`; `inPlace` = `false`; `workspacePath` = `""`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`.

The roadmap Phase 4 (G1) single front door. A read-only `coding_agent` classifies `request` against the repo (kind, size, confidence, a normalized instruction, a decomposition, frozen contract paths); `intake_route` folds that into a route — never pausing for a human (low confidence proceeds on the best classification and is flagged `lowConfidence`) — by two-dimensional routing on kind and mode — `bug` → `bug_fix`, a multi-item/coupled request → `team_build`, one small piece → `code_parallel`, `greenfield` → project_bootstrap (Phase 5). Owns no branch; each routed stage workflow owns its own workspace.

The execution mode is read off the decomposition (closure plan WP3, H12/H14): `intake_route` runs the decomposition gate on the planner's items — one item → `single` (`code_parallel`); several items with no `dependsOn` edge and no shared file → `parallel` (`code_parallel` on the precomputed plan, parallel-independent execution with no boards); any edge or shared file → `team` (`team_build`, with `bestOf` candidate forks per item). A bad cut — a dependency cycle or unknown dependency, an item with no files, duplicate scope, or two unordered items overlapping on half or more of their files — routes to `replan`: the classifier is re-run with the precise reasons, up to `maxReplan` times, and a plan still rejected ends `PLAN_REJECTED` before any agent is spawned. The output carries `mode` and `routeEvidence` (edges, shared files, overlaps) so the routing boundary is auditable (Gate 0).

## `project_bootstrap`

Required: `repoPath`.

Optional: `description` = `""`; `firstFeature` = `""`; `stack` = `""`; `verificationCommand` = `[{"argv": ["make", "test"]}]`; `codeModel` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `10`; `maxFixAttempts` = `2`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`; `branchName` = `""`; `branchRunId` = `""`; `inPlace` = `false`; `workspacePath` = `""`.

The roadmap Phase 5 (G3) greenfield on-ramp. `sdlc_intake` routes a request it classifies as `greenfield` here, with the normalized request as `firstFeature`. The project is built on its own run branch in a worktree (`branchName`, default prefix `bootstrap`), so your checkout is untouched, and the run reports that branch like any other run. `bootstrap_init` git-inits the (empty) `repoPath` with an empty base; a `coding_agent` scaffolds a minimal buildable skeleton for `description` (hints: `stack`) with at least one passing test and the verification command documented in `AGENTS.md`; the `commit` worker commits it; `test_cycle` runs `verificationCommand` and repairs a failing skeleton up to `maxFixAttempts` times (`SKELETON_NOT_GREEN` if it still fails). If a `firstFeature` is given, `code_parallel` adds it on top and a second `test_cycle` must pass `verificationCommand` on the final commit, with repair; `bootstrap_outcome` emits `BOOTSTRAPPED` (green skeleton + feature delivered), `SKELETON_READY` (no feature requested), `FEATURE_INCOMPLETE`, or `SKELETON_NOT_GREEN`.

## `cc_program`

Required: `repoPath`.

Optional: `description` = `""`; `subsystems` = `[]`; `contractPaths` = `[]`; `ceilingUsd` = `60`; `maxWidth` = `6`; `maxWaves` = `16`; `maxReplan` = `2`; `maxDepth` = `2`; `depth` = `0`; `parentCorrelationId` = `""`; `subsystemId` = `""`; `bestOf` = `1`; `authorAcceptanceTests` = `true`; `codeModel` = `""`; `maxTurns` = `250`; `maxBudgetUsd` = `10`; `maxFixAttempts` = `2`; `branchName` = `"program"`; `branchRunId` = `""`; `inPlace` = `false`; `workspacePath` = `""`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`; `maxTeamAttempts` = `2`.

The roadmap Phase 6 (G4 + G5), built as **teams of teams** (closure plan WP5, H3). `repo_memory_read` reads on-branch persistent knowledge and decays stale lessons (H10); a read-only `coding_agent` plans `description` into subsystems (each optionally split into its team's `items`), and the decomposition gate re-plans a bad cut up to `maxReplan` times (`PROGRAM_PLAN_REJECTED` if it never passes, `PROGRAM_PLAN_CYCLIC` for a non-DAG). Given `subsystems` (a nested program) planning is skipped. The planner slices before any design exists, so once a rank delivers design documents (a `docs` subsystem) the subsystems still to build are re-derived from them: `program_apply_rank` asks for a work breakdown (`breakdown: yes`, naming the documents actually on the branch), a read-only `coding_agent` (`rank_breakdown`, in the run's workspace) returns each remaining subsystem's items from the design's file layout, names and interfaces, and the next `program_rank_build` folds it in once per planner session — new items and files on the program fact and board, the planner's spend on the budget, a resolved `breakdown` notice per re-sliced subsystem. Twice a pre-design item demanded what the design then forbade and the reviewers enforced the design against the plan's own work (st-lru's hooks in a file another item owned, run `fc6467b7`; an r-value item creating the `resp/value.go` the design prohibits, run `a062184f`). `program_rank_init` seeds a **program board** — one item per subsystem — and computes build ranks whose members share no file; each rank is built **concurrently by one child `team_build` per subsystem** (its own board, Lead, monitor, reviewers, acceptance tests) on the shared program branch, with program-scoped file ownership and serialized continuous integration; a subsystem that is itself a program runs a child `cc_program` while `depth` < `maxDepth`. Each child gets an equal share of the remaining `ceilingUsd`; its spend, verdict and rollup are folded onto the program board by `program_apply_rank`, and the next rank starts only when every subsystem of this one delivered. `test_cycle` (full) gates cross-subsystem integration green; `program_outcome` reports `PROGRAM_DELIVERED`/`PROGRAM_INCOMPLETE` with the ranks and each child run; `repo_memory_write` persists lessons. A team that ends INCOMPLETE does not end the program at once: once its whole rank has finished, the program re-runs it (`maxTeamAttempts`, default `2`: the first attempt and one re-run), re-forking only that team with the remaining budget and posting the team's own report as a directive to `subsystem:<id>`, which its implementers and reviewers read; the work already on the branch stays, and an item whose frozen acceptance test is already on the branch reuses it as its oracle (`reused` on `team_acceptance_build`; re-authored it would pass at once, be judged tautological and be deleted). A team still INCOMPLETE after its last attempt blocks its subsystem as before. A program's team is gated on its own oracle: its final `test_cycle` runs its done items' frozen-test commands (`testCommands` = `team_apply_reviews.gateCommands`), not a repository-wide command that would also run sibling teams' tests while they are still being built; cross-team checks are the integration oracle's and the program's final suite's. And `test_run` runs every selected command even after one fails, so each leaves evidence. All three are from run `037e1cbe`: the repository documented no test command, the pgtype team's final check inferred a bare root `pytest`, failed on the parser team's tests (that team was still building in the same rank) and stopped before pgtype's own tests ran; its repair agent diagnosed exactly that, but nothing acted on it and the program ended at 2/9. The breakdown's output schema spells out each item (`id`, `description`, `files`, `dependsOn`, `acceptanceCriteria` and `reviewCriteria` as arrays of strings, `deliverable`): typed as bare objects, a paragraph passed as `acceptanceCriteria` and every team of run `90e5e3aa` built without frozen tests. The breakdown asks for a source file's test file alongside it in an item's `files` (sv-registry was widened to its own `registry_test.go` a review round late, run `688f426b`). The decomposition gate rejects a program plan with a fatal defect whatever its size: a placeholder item (a description or acceptance criterion that is just "test", "ok", "tbd" and the like) and, for `cc_program` (`requireCode` on `program_parse`), a plan with no `code` deliverable — a one-item plan used to route `single` and could never be sent back. The agent runner also keeps the answer an agent actually wrote when the output validator accepts a placeholder it sent afterwards: a rejected payload that untangles into a schema-valid one at least four times the size replaces the accepted one (`structured_salvaged: placeholder`). The planner wrote its six-subsystem plan twice as tagged text inside `summary`, then sent `{"id": "design", "description": "test", "acceptanceCriteria": ["ok"]}`, which validated; the gate routed the one item, the program built that document in ten minutes and reported itself `PROGRAM_DELIVERED` (run `b8dc259b`).


**Result branch.** `branchName` names the branch a run's work lands on; the run's short id is appended so two runs never share one (`swarm/pg-compatible-database-037e1cbe`). The console fills it from the request (`swarm/<words of the request>`); left at the default the branch is `program-<run id>`. The run's checkout under `.cc-worktrees/` holds the branch until a follow-up run continues from it (Continue takes the branch over from a finished run's clean checkout) or the next run on the repository prunes finished runs' checkouts; the branch keeps every commit either way, and `git log`, `git diff` and `git merge` on it work from your own checkout.

## `run_retrospective` (internal)

Required: none.

Optional: `correlationId` = `""`; `workflowId` = `""`; `repoPath` = `""`; `workspacePath` = `""`; `verdict` = `""`; `report` = `""`; `parentCorrelationId` = `""`; `codeModel` = `""`.

The retrospective every swarm run ends with. Its agent reads the run's workspace (`workspacePath`: the run's branch with everything the teams merged; a failure workflow is not handed it, so `retro_evidence` reads it back from the run's own `workspace` task), never the source checkout, which is still on its original branch and shows none of the run's work. The agent's whole StructuredOutput payloads travel as `structuredAttempts`, and `retro_record` records the most substantive candidate after untangling a payload whose learnings were emitted as tagged text inside the summary string (the validator rejected every real payload of run `a062184f` and a placeholder learning "a" was recorded); a learning shorter than 10 characters is never recorded. `team_build` and `cc_program` call it as their last step (and before their early planning exits), name it as their `failureWorkflow` (Conductor runs that only when a run FAILS, passing the failed run's input plus `workflowId`), and the harness's own terminate (console, TUI `t t`, a "kill the run" message) starts it right after terminating, because Conductor never runs a failure workflow for an API termination. `retro_evidence` gathers what actually happened on the run's board and its child teams' boards (blocked items and their mechanisms, pushback and exemptions, rejected contract edits, review rework, human feedback, lessons) — a child team of a program is skipped, the program's retrospective covers it; a read-only `coding_agent` distils at most ten durable learnings, each scoped `repo` (keyed by the origin URL, else the main checkout), `environment` (this host's toolchain), `language` (a language/framework convention) or `process`; `retro_record` appends them to the shared learnings board (`_swarm_learnings`, deduplicated, retractable). Learnings live on the blackboard, not the run's branch, so a failed or terminated run's lessons survive. Later runs read the relevant ones: `repo_memory_read` adds them to the program planner's digest and every `team_build` standup adds them to each implementer's prompt.

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

Optional: `repo` = `""`; `repoPath` = `""`; `workspacePath` = `""`; `instruction` = `""`; `issueNumber` = `0`; `keepWorktree` = `true`; `changeBranch` = `""`; `inPlace` = `false`; `contextPaths` = `[]`; `maxApprovalRevisions` = `2`; `prBase` = `"main"`; `prTitle` = `""`; `prBody` = `""`; `prDraft` = `false`; `designDir` = `"docs/design"`; `designAgent` = `"claude"`; `designHumanApproval` = `true`; `designModel` = `""`; `planAgent` = `"claude"`; `planModel` = `""`; `codeAgent` = `"claude"`; `codeModel` = `""`; `reviewAgent` = `"claude"`; `reviewModel` = `""`; `maxTurns` = `500`; `maxBudgetUsd` = `50`; `maxFixAttempts` = `4`; `maxTasks` = `25`; `maxParallelism` = `6`; `maxWaves` = `20`; `designMaxRevisions` = `5`; `planMaxRevisions` = `5`; `finalMaxRevisions` = `3`; `useImportedPlan` = `false`; `importedPlan` = `{}`; `importedDesignLocation` = `""`; `specContextPath` = `""`; `designPromptTemplate` = `""`; `designPromptTemplateSource` = `""`; `planPromptTemplate` = `""`; `planPromptTemplateSource` = `""`; `codePromptTemplate` = `""`; `codePromptTemplateSource` = `""`; `reviewPromptTemplate` = `""`; `reviewPromptTemplateSource` = `""`; `revisionPromptTemplate` = `""`; `revisionPromptTemplateSource` = `""`; `allowAgentTestPlan` = `true`; `allowAgentAuthoredTests` = `true`.

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

Publication is a human decision on every run, not an input. There is no `createPr` or
`requirePrApproval` flag: the verified candidate's PR draft always goes to `pr_draft_approval` --
the same human-approval-with-bounded-revisions sub-workflow `issue_to_pr`'s `approvePr` uses --
and only an explicit approval there pushes the branch and opens the PR. Anything else
(`suppressed`, `blocked`, `verification_blocked`, `revision_exhausted`) pushes nothing at all and
leaves the branch local, with `publicationState` reporting that approval state as the reason.
This holds for `inPlace` runs too: an approved draft pushes the run-owned branch from the
operator's own checkout. `maxApprovalRevisions` (default `2`) bounds how many human-requested
revision rounds `pr_draft_approval` will run before a still-pending approval is relabeled
`revision_exhausted`.

Because the gate is unconditional, every campaign parks at `pr_gate` until a human answers it.
An unattended driver must signal that gate -- `scripts/run_real_e2e.py`'s `drive_campaign` does,
alongside the four phase checkpoints.

Two independent choices, each "at least one of," both runtime-enforced by `campaign_defaults`/
`campaign_workspace` (`workspace_prepare`) rejecting the run at start if unmet -- neither is
schema-required because either alone is legitimate:

- **What to build:** `instruction` (free text) or `issueNumber` (a GitHub issue number, requires
  `repo`). When `issueNumber` is set, the campaign fetches that issue's title/body -- plus any
  issue/PR/doc/CI links the body itself references, resolved the same untrusted-evidence way
  `pr_review` resolves links in PR feedback -- and uses that as the effective instruction instead.
  The originating issue is surfaced in output (`issueNumber`/`issueTitle`/`issueUrl`), and an
  approved PR body gets a trailing `Closes #<n>`.
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
first and only wants to publish once a human is satisfied can reuse it (`feature_campaign` calls
it unconditionally on every run -- see above). A human must explicitly approve the
draft via the `pr_gate` WAIT task, optionally requesting up to `maxApprovalRevisions` bounded,
independently re-verified revisions (via `code_parallel`) first, resolving to exactly one of:
`approved`, `suppressed`, `verification_blocked`, `revision_exhausted`. A decision the gate does not
recognise (anything but `approve`/`revise`/`stop`, or a `revise` with no feedback) never ends the run:
the state stays `pending`, the loop re-opens `pr_gate` with `draft.unrecognizedDecision` naming what
arrived, and that round counts against `maxApprovalRevisions`.
`callerWorkflow` must be the caller's own top-level workflow name (e.g. `"issue_to_pr"`,
`"feature_campaign"`) -- the TUI's approval dispatch branches on it, and without it a gate living
in this sub-workflow would report its own name instead of the caller's.

## `ship_run`

Required: `repoPath`, `branch`, `head`, `title`.

Optional: `base` = `""`; `summary` = `""`; `draft` = `false`; `remote` = `"origin"`.

Ships a finished swarm run: `git_push` pushes the run's result branch to `remote`, bound to `head` (the commit the result card showed; a branch that moved since is refused), then `pr_create` opens a pull request from it into `base` (empty: the repository's default branch), with `summary` as the body's single Summary paragraph. An open pull request for the branch is reused and its summary updated. The console's **Open pull request** and the TUI's `P` start it for a run whose repository has a GitHub remote; a repository without one is merged locally instead (console **Merge into &lt;branch&gt;**, TUI `M`), which never needs a workflow.

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
