# Simplicity Policy

- Default to the simplest design that meets the actual requirement. Only add complexity — a new task, branch, gate, flag, sub-workflow, or file — when the existing structure genuinely cannot express the requirement, not because it is convenient, symmetrical, or "might be needed later."
- Before adding a `SWITCH` branch, task, or workflow input, first check whether an existing task, flag, or code path can be extended or reused instead of adding a new one.
- A workflow whose task/branch count keeps growing is a signal to stop and simplify the design, not a signal to add the next branch. Re-read the whole workflow before extending it again; if it is hard to hold in your head, that is the defect to fix, not a reason to add more state to track.
- Prefer one task that does slightly more over two tasks wired together, when the second task has no independent retry, tracking, or reuse value of its own.
- When a change is reviewed, justify new complexity by name: state the concrete requirement that could not be met without the new task/branch/flag. "It seemed safer" or "for consistency" is not sufficient justification on its own.
- Simplifying an existing workflow (removing a task, branch, or flag that no longer earns its complexity) is a welcome change in its own right, independent of any feature it enables.

# Conductor Workflow Version Policy

- Every workflow definition in this repository must use `"version": 1`.
- Update workflow version 1 in place. Do not create, register, or recommend a new workflow version.
- Every `SUB_WORKFLOW` reference, including dynamically generated task definitions, must pin version 1.
- Keep exactly one registered definition per workflow name: version 1. Remove any accidentally registered higher version.
- Before registering workflows, verify the local definitions and all sub-workflow references comply with this policy.

# Conductor Timeout Policy

- Do not add or recommend workflow-level or task-definition timeouts.
- A zero or unset timeout is intentional in this repository and must not be reported as a review finding.
- Handle missing or unavailable workers separately through worker registration, queue monitoring, and operational diagnostics; do not use timeouts as the remedy.
- Do not add hidden execution deadlines in Python, shell, HTTP clients, subprocess helpers, workers, or TUI orchestration. Commands and requests run until they complete or the user explicitly cancels them.
- Do not add `timeout`, `timeout_s`, `timeout_seconds`, `asyncio.wait_for`, shell `timeout`, curl `--max-time`/`--connect-timeout`, or equivalent deadline mechanisms to execution paths.
- Queue polling intervals, retry backoff, token-expiration timestamps, and UI notification display durations are not execution deadlines. They may control when to poll again or how long presentation remains visible, but must never abort running work.
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
