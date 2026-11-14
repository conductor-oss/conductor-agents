# Gap-Closure Policy

- Never write "honestly", "to be honest" or "honest caveats", and never use that framing as a lead-in to
  deliver less than asked. State the fact plainly and continue with the fix. Applies to agent output,
  reviews, commit messages, docs and task reports.

- **Never silently drop part of an ask.** Before reporting a task done, list every part of the request and check each against what was built. A part not built is said in the first lines of the report, by name ("hill-climbing loop: not built"), never implied by a hedge such as "partly" or buried as a closing remark, and then built unless the asker says otherwise. Each part reported done comes with its proof (a test, a run, a measurement). An effort ladder was reported as the adaptive loop that was asked for, and the hill-climbing half surfaced only when asked again (2026-11-11).
- A known gap, limitation, or "caveat" is **work to finish, not a note to hand off**. When you find one, drive it to a fix or a concrete, verified resolution in the same effort — do not deliver the gap as a standing caveat and move on. That is the job.
- Do not end a deliverable with a list of unresolved "honest caveats". If something is not proven, prove it; if a path is untested, test it; if a mechanism only works in theory, exercise it end to end until it demonstrably works.
- This does **not** relax the honesty policies below. Outcomes are still reported truthfully and failures are still named by their precise mechanism. The rule is about *resolution*, not concealment: never hide a problem, and never leave one open that you could have closed. A truthful "it works, here is the proof" is the goal; a truthful "it might not work, caveat" is an unfinished task.
- When a limit is genuinely external and cannot be removed in-repo (for example a physical-host or third-party-service ceiling), do not frame it as a vague caveat: name the exact limit, the concrete step that would remove it, and prove the largest increment that *is* reachable now. An external limit is a scoped next action, not a hedge.
- Applies everywhere a caveat might otherwise appear: agent responses, PR/commit descriptions, result docs, task output, and review summaries.

# Simplicity Policy

- Default to the simplest design that meets the actual requirement. Only add complexity — a new task, branch, gate, flag, sub-workflow, or file — when the existing structure genuinely cannot express the requirement, not because it is convenient, symmetrical, or "might be needed later."
- Before adding a `SWITCH` branch, task, or workflow input, first check whether an existing task, flag, or code path can be extended or reused instead of adding a new one.
- A workflow whose task/branch count keeps growing is a signal to stop and simplify the design, not a signal to add the next branch. Re-read the whole workflow before extending it again; if it is hard to hold in your head, that is the defect to fix, not a reason to add more state to track.
- Prefer one task that does slightly more over two tasks wired together, when the second task has no independent retry, tracking, or reuse value of its own.
- When a change is reviewed, justify new complexity by name: state the concrete requirement that could not be met without the new task/branch/flag. "It seemed safer" or "for consistency" is not sufficient justification on its own.
- Simplifying an existing workflow (removing a task, branch, or flag that no longer earns its complexity) is a welcome change in its own right, independent of any feature it enables.

# Swarm Human-Role Policy

- Humans are passive actors in the agent swarm (`sdlc_intake`, `team_build`, `cc_program` and anything they route to): they monitor, give feedback, and can stop an agent or terminate a run. They never approve.
- No swarm workflow may contain a `WAIT` task, a human-approval gate, or a `humanAvailable`-style input that makes progress depend on a human answer. Every decision the swarm faces is made by the swarm itself: a frozen-contract edit is rejected by the Lead, except an amendment of only frozen acceptance tests, which is held unmerged and decided by the item's peer reviewer (approve → merged and re-frozen at the merged head; reject → discarded), an honored agent pushback exempts that item from that one directive, a quarantine or starvation ends the run naming its mechanism.
- Escalations are notices for awareness, not requests for decisions. Human feedback (a plain-language message, a nudge, a stop, a reopen, a constraint lift/replace) is advisory input the team takes at its next tool boundary or dispatch; it never unblocks anything because nothing is ever blocked on a human.
- Enforcement: `workers/tests/test_team_cycle.py::test_swarm_workflows_never_wait_for_a_human` fails if a swarm workflow gains a `WAIT` or a `humanAvailable` input.

# Conductor Workflow Version Policy

This repository does not use workflow versioning. There is exactly one version of every
workflow -- version 1 -- on disk and on the server.

- Every workflow definition in this repository must use `"version": 1`.
- Update workflow version 1 in place. Do not create, register, or recommend a new workflow version. "Bump the version" is never the answer to a definition change, a breaking change, or a migration.
- Never pass a version when starting a workflow: no `?version=` on `POST /workflow/{name}`, no `version` in a `startWorkflowRequest`, `START_WORKFLOW` task, or `subWorkflowParam` (static or dynamically generated). The server starts the version it has registered.
- Keep exactly one registered definition per workflow name: version 1. A registered v2 or higher is a defect to delete, not a definition to maintain.

Why one registered version is strict rather than stylistic: an unversioned start runs the
**highest** registered version, so a single obsolete definition left on the server silently
replaces the local one for every launch, and the run's input is merged with the obsolete
`inputTemplate`. A stale `feature_campaign` v2 whose `campaign_workspace` never wired `repoUrl`
shadowed the current v1 for weeks and failed every clone-from-GitHub campaign at
`workspace_prepare` with "requires repoPath or repoUrl", while the definition on disk was
correct the whole time (execution `f69afa80-6c1b-48c3-a116-cffb688cfb4e`).

How it is kept that way:

- `workers/register.sh` does not gate registration on task types, versions, or task
  definitions. After registering it deletes every registered version other than the local one,
  keeping any version that still has a `RUNNING`/`PAUSED` execution.
- `scripts/audit_harness_invariants.py` fails with `REGISTERED_VERSION` when the server holds
  any version besides the local one. It is skipped, not passed, when no server is reachable.
- Pinning a version at start time is not a substitute: a pinned start 404s whenever the
  registered version differs, while the cleanup above already guarantees the unversioned
  start resolves to v1.

See `docs/workflow-versioning.md` for the deployment procedure.

# Conductor Timeout Policy

- Do not add or recommend workflow-level or task-definition timeouts.
- A zero or unset timeout is intentional in this repository and must not be reported as a review finding.
- Handle missing or unavailable workers separately through worker registration, queue monitoring, and operational diagnostics; do not use timeouts as the remedy.
- Do not add hidden execution deadlines in Python, shell, HTTP clients, subprocess helpers, workers, or TUI orchestration. Commands and requests run until they complete or the user explicitly cancels them.
- Do not add `timeout`, `timeout_s`, `timeout_seconds`, `asyncio.wait_for`, shell `timeout`, curl `--max-time`/`--connect-timeout`, or equivalent deadline mechanisms to execution paths.
- Queue polling intervals, retry backoff, token-expiration timestamps, and UI notification display durations are not execution deadlines. They may control when to poll again or how long presentation remains visible, but must never abort running work.
- A batch-poll request's wait for its response is a polling interval, not a deadline: the server answers within its own long-poll timeout, so `common/polling.py` bounds the wait to that timeout plus a grace period and the SDK simply polls again. Without it a lost response parks the task type's only poller in `recv` forever while its queue fills (coding_agent poller, 2026-11-07). The bound covers the poll call only; task execution, lease renewal and result updates carry none.
- Every active worker task must hold the platform's idle-sleep inhibition assertion for its full execution. Lease renewal cannot protect a task while its worker process is suspended; prevent idle sleep instead of adding a longer deadline.

# JQ Usage Policy

- JQ is considered harmful in this repository. A `JSON_JQ_TRANSFORM` is brittle, fails on edge cases that are hard to reproduce and hard to fix, and a throwing expression ends the whole workflow `FAILED`. This is measured, not theoretical: an unprotected throwing JQ task fails its workflow, while the same task marked `optional: true` completes.
- Treat JQ as an extreme exception, not the norm. Do not reach for a `JSON_JQ_TRANSFORM` because it is convenient.
- Prefer, in this order: compute the value in a worker task and return it as structured output; use `SET_VARIABLE` to carry state; use a `SWITCH` to route. A `SWITCH` routes rather than executes and cannot fail, which makes it strictly safer than a JQ expression that computes the same decision.
- When a JQ task is genuinely unavoidable, it must be `optional: true`, and every input it reads must be type-guarded (`(.x | type) == "string"`) and blank-guarded (`gsub("[[:space:]]"; "")`) so a null or missing upstream value cannot throw.
- Every JQ expression carries a mandatory cost: three fixture cases, one in each of `workers/tests/fixtures/jq_conductor_{cases,adversarial_cases,third_pass_cases}.json`. That cost is a deliberate deterrent. Adding a JQ task is a decision to be justified, and removing one is an improvement.
- New workflows should aim for zero JQ tasks. Reducing the JQ count in an existing workflow is a welcome change on its own.
- This repository holds itself to zero registered `JSON_JQ_TRANSFORM` tasks across every workflow. Do not reintroduce one; extend an existing `common/<workflow>.py` pure-logic module (see `common/test_plan.py`, `common/code_parallel.py`, `common/issue_to_pr.py`, `common/gate_decision.py`, and siblings) and add a thin `@worker_task` wrapper instead.

# Repository Command Runtime Policy

- Every production child process must use `workers/common/exec.py`, `workers/common/check_execution.py`, or explicitly pass `check_execution.inherited_environment()`.
- Do not add a raw `subprocess.run`, `subprocess.Popen`, `os.system`, or equivalent command-launch path that bypasses the shared runtime environment.
- The shared worker contract covers Java; Go; Python (`python` and `python3`); Ruby (`ruby`, `bundle`, and `rake`); and TypeScript through Node, npm, and npx in both inherited and isolated verification environments.
- Use the repository-pinned TypeScript compiler through npm/npx. Do not depend on or install an unpinned global `tsc`.
- After changing a command-launch path, re-register and execute the `runtime_health` workflow. A valid proof requires both `executionHealthy` and `verificationHealthy` to be `true` in a completed Conductor execution.

# Pull Request Description Policy

- Every harness-created or reused pull request body must contain exactly one section: `## Summary` followed by one concise paragraph of at most 500 characters.
- Do not add verification reports, subtask lists, warnings, issue-closing directives, automation markers, cost data, or other sections to a pull request body.
- All PR-producing workflows must route through `pr_create`; do not call GitHub PR creation directly.
- For issue-backed workflows, when `.github/ISSUE_TEMPLATE` exists, use the first substantive primary description field from the filled issue as the summary and discard all remaining template fields.
- Never use `gh pr create --fill`; it can reintroduce repository template sections outside the canonical summary.

# Pull Request Review Policy

- A clean `pr_review` result is exactly `LGTM`, contains no inline comments, and is submitted as a GitHub `APPROVE` review.
- Review comments must request a concrete change. Do not publish praise, diff narration, restatements, optional polish, nits, or conversational filler.
- In the TUI, Approve posts `LGTM` plus only the selected inline comments and approves the PR; Request changes posts the user's feedback as `REQUEST_CHANGES` without approval; Later leaves the WAIT task open and publishes nothing.
- Human Request changes feedback is final publication content for that decision. Do not ask an agent to rewrite the review and do not open another review-approval loop.

# Failure Terminology Policy

- Never use vague labels for workflow defects. Name the concrete failure mechanism.
- Name the concrete failure instead: invalid task wiring, mismatched input/output shape, incorrect repair scope, incorrect control flow, lost output, or another precise mechanism supported by execution evidence.
- Reports must identify the producer, consumer, actual value or state, expected value or state, and resulting workflow behavior. Do not hide the mechanism behind generic terminology.
- This applies everywhere: agent responses, workflow output, task output, TUI text, logs, code comments, documentation, and review summaries.

# Repository Verification Command

- The repository-wide verification command for this repository is `make test`. It is recorded here,
  in the agent guide, because that is where the harness looks: `test_cycle`'s full-mode
  `test_discover` reads `AGENTS.md` -> `AGENT.md` -> `CLAUDE.md` -> `CONTRIBUTING.md` -> `README.md`
  and prefers a command documented there (`selection: documented-guide`) over build-system
  inference. A guide that documents no command is not a neutral omission -- it silently hands the
  test loop whatever inference guesses.
- Keep exactly one extractable command in the guide. Discovery collects every backticked snippet
  that validates as a supported argv and runs all of them, so a second one is a second full-suite
  run on every candidate. Other targets (make ci, make lint, make validate) are deliberately not
  written as runnable snippets here.
- This is what kept feature_campaign's PRs permanently in draft. With no documented command,
  inference fell back to a bare root pytest run, which died in collection with
  "No module named 'common'" (`tui/tests` needs the `sys.path` entry that
  `workers/tests/conftest.py` adds, and given no arguments pytest reaches that conftest only after
  collecting `tui/tests`). A failed test cycle leaves `testsPassed` false, and
  `resolve_campaign_publication_plan` forces `draft: true` whenever tests did not pass -- so every
  campaign published a draft PR while the suite itself was fine.
- `pyproject.toml`'s `testpaths` keeps that inference fallback honest rather than relying on this
  guide alone: a candidate commit that edits `AGENTS.md` excludes it from its own run's discovery
  by design (candidate-authored instructions must not grant command-execution authority), and the
  fallback must still be a command that actually runs.

Enforcement, so the rule cannot rot back into advice: `workers/tests/test_verification.py` runs
the real extractor (`_candidate_from_guide`) and the real `discover_commands` against this file
and fails when the guide stops yielding exactly one command, or when discovery stops selecting it
over inference. A second check holds `testpaths` to both test roots.

# Test Execution Policy

- Verification never blocks on discovery. `test_discover` climbs a ladder and runs the first rung that exists: the exact tests the changed files map to, then the affected package's own test command, then the repository suite (the command documented in this guide, then build-system inference, a heavy suite last). A candidate that changes only documentation ends `no_tests_required`; a repository with no test command at all ends `no_tests_found` with the reason named. Both are reported by name and count as passed because nothing could fail; neither is ever converted into a run that did not happen.
- There is no "repository-wide fallback is prohibited" rule, no `command_discovery_blocked` or `configuration_blocked` state, and no agent-proposed test plan or agent-authored test gate behind a flag a caller must forward. A changed file no per-language planner maps is covered by the next rung, never refused. Reintroducing any of them is a regression, not a safety feature.
- A heavy-suite marker (`acceptance`, `e2e`, `integration`, …) orders candidates, it does not refuse them: a lighter command is preferred when one exists, and the heavy suite runs when it is the only one. The marker must never be scanned against the VALUE of a single-target test-filter flag (`--tests`, `-Dtest=`, `-k`, `--filter`, `-R`, `TEST=`/`SPEC=`): that value names one already-selected test, picked mechanically from the changed file. The exemption is shape/position-based (which argv slot the token occupies), never string-based.
- Verification commands are still validated for safety, never for breadth: a plain argv (no shell syntax), a known build or test entrypoint, one goal. That is what keeps a candidate from steering the harness into arbitrary execution; scope is the ladder's job.
- `scripts/audit_harness_invariants.py`'s `HEAVY_MARKER_FALSE_BLOCK` check enforces the filter-value rule structurally.

