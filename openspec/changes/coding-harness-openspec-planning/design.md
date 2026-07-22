## Context

`coding-harness` runs on Conductor. Today, `code_parallel.json` plans work with two agent-driven steps that bypass Conductor's own determinism:

- `design_docs.json`: a `DO_WHILE` loop where a coding agent freely writes `docs/design/*.md`, then either a `HUMAN` task or an AI-judge `coding_agent` task approves/rejects, looping until approved or `designMaxIterations` is hit.
- `plan` (inside `code_parallel.json`): a single `coding_agent` SIMPLE task that reads the instruction (and `docs/design/` if present) and returns a structured-output JSON array of subtasks (`id`, `description`, `files`, `testCmd`), which `build_forks` (JSON_JQ_TRANSFORM) reshapes into a `FORK_JOIN_DYNAMIC` fan-out. `code_subtask.json` then runs `worktree_add` → `coding_agent` → `commit` per subtask, and `merge_worktrees` merges branches back.

Nothing enforces that the LLM's subtask list is actually independent/file-disjoint; a bad decomposition only surfaces as a merge conflict late in `merge_worktrees`. The design and plan steps are both single opaque agent calls — there's no intermediate state Conductor can inspect, retry, or resume against.

OpenSpec provides a `spec-driven` schema (`proposal → specs → design → tasks`) exposed through a scriptable CLI (`openspec new change`, `openspec instructions <artifact> --json`, `openspec status --json`) that already returns the dependency graph, per-artifact templates/instructions, and completion status as data. This lets Conductor drive the *sequencing* deterministically while still using an agent only for the *content* of each artifact.

## Goals / Non-Goals

**Goals:**
- Replace `design_docs.json` and the `plan` step with a Conductor sub-workflow (`openspec_plan`) whose control flow (which artifact runs next, when planning is done) is driven by CLI output, not agent judgment — while reusing `design_docs.json`'s existing human-or-AI-judge `DO_WHILE` review loop unchanged in mechanism, retargeted at the generated OpenSpec artifacts.
- Guarantee that the subtasks fed into `code_parallel.json`'s existing `FORK_JOIN_DYNAMIC` fan-out are file-disjoint, by constraining `tasks.md` generation and deterministically parsing it rather than trusting free-form LLM JSON.
- Leave `code_subtask.json`, `FORK_JOIN_DYNAMIC`, and `merge_worktrees` untouched — the fan-out contract (`subtasks[]` → `dynamicTasks`/`dynamicTasksInput`) is preserved.

**Non-Goals:**
- Not changing how an individual subtask is implemented (`coding_agent` + `code.md` prompt in `code_subtask.json` stays as-is).
- Not building a general-purpose OpenSpec UI or archival flow for the target repo; `openspec_plan` only needs enough of the CLI to scaffold, generate, and status-check one change per `code_parallel` run.
- Not implementing OpenSpec's `apply`/`archive` lifecycle — `code_parallel` already has its own implementation loop (the fan-out); OpenSpec is used purely for the planning artifacts, and this change does not archive the change it creates in the target repo.

## Decisions

### D1: New task defs wrap the `openspec` CLI as typed SIMPLE tasks
Add worker task defs (alongside the existing `taskdefs/`) — e.g. `openspec_new_change`, `openspec_instructions`, `openspec_status` — each a thin subprocess wrapper around the corresponding CLI command with `--json`, parsing stdout into task output. This mirrors the existing pattern (`git_clone.json`, `worktree_add.json`, etc. already wrap git/gh CLIs the same way) rather than introducing a new integration style.

*Alternative considered*: let the coding agent call `openspec` as a shell tool itself (per the "agent-driven, no dedicated worker" option). Rejected because the explicit goal is determinism — Conductor's execution history should show a typed `openspec_status` task with structured input/output, not an agent's shell transcript, so failures/retries/resumption are inspectable the same way `git_clone` or `worktree_add` failures are today.

### D2: `openspec_plan` sub-workflow structure
A new `openspec_plan.json` sub-workflow that ports `design_docs.json`'s existing control flow onto the OpenSpec artifact set, changing only *what* is generated/reviewed, not the review mechanism itself:
1. `openspec_new_change` — scaffold the change in the target repo (`repoPath`), name derived from `changeBranch`/instruction slug. Runs once, before the loop.
2. A bounded loop (`DO_WHILE`, capped by `openspecMaxIterations`, same mechanism as today's `designMaxIterations`), each iteration:
   a. Calls `openspec_status`, JQ-selects the next `ready` artifact(s) (all pending artifacts on the first pass; whichever the prior feedback affects on a repeat pass), calls `openspec_instructions` for each, and runs a `coding_agent` SIMPLE task per artifact using the returned `template`/`instruction` as prompt context to (re)write the artifact file, addressing any prior feedback.
   b. A `SWITCH` on the `humanApproval` input, structurally identical to `design_docs.json`'s `review_mode` SWITCH: `true` → a `HUMAN` task presents the generated artifacts and captures `approved`/`feedback`, exactly like today's `design_review` task. `default` → a read-only AI-judge `coding_agent` task (tools `Read`/`Grep`/`Glob`) judges the artifacts against the instruction and returns `{approved, feedback}` via structured output, exactly like today's `design_judge` task.
   c. Loop condition: continue while `!approved && iteration < openspecMaxIterations` — identical to `design_docs.json`'s `design_loop` condition.
3. After the loop, a `SWITCH` on the final `approved` value: `true` → `openspec_tasks_to_subtasks`, a deterministic parser task reading the generated `tasks.md`. `false` (iteration cap exhausted without approval) → `TERMINATE` with the reviewer's feedback in `workflowOutput`, matching `design_docs.json`'s existing `design_not_approved` termination pattern exactly.

Output: `subtasks[]` in the same shape `code_parallel.json`'s old `plan` step produced, so `build_forks` needs only its input source changed (`${openspec_plan.output.subtasks}` instead of `${plan.output.structured.subtasks}`).

*Alternative considered*: drop the AI-judge branch and make review a simple optional, one-shot human gate. Rejected per explicit direction — keep `design_docs.json`'s existing human-or-AI-judge loop exactly as it behaves today; only the artifacts being generated and reviewed change, not the review mechanism.

### D3: `tasks.md` independence constraint + parser contract
Extend the `tasks` artifact's generation prompt (via the target repo's `openspec/config.yaml` `rules.tasks`, injected by `openspec_instructions`'s consumer) to require: one `## N. <slug>` heading per independent unit of work, a `Files:` line and a `Test:` line per heading, and no file repeated across headings. The `openspec_tasks_to_subtasks` parser task then:
- Splits `tasks.md` on `## N. ` headings.
- Maps each heading to `{id: slug(heading), description: joined checkbox bullets, files: parsed Files: line, testCmd: parsed Test: line}`.
- Fails closed (workflow `TERMINATE`) if any file appears under more than one heading, or if a heading is missing `Files:`/`Test:`.

*Alternative considered*: have the `tasks` artifact-generation agent emit structured JSON output (like the old `plan` step) instead of markdown, and skip parsing. Rejected because `tasks.md` is OpenSpec's own tracked artifact (`apply.tracks: tasks.md`, checkbox-parsed) — keeping it as the single source of truth for both "what to build" (readable by humans/OpenSpec) and "how to fan out" (parsed by Conductor) avoids two divergent representations of the same plan.

### D4: `issue_to_pr.json` and other callers
`issue_to_pr.json` (and any other workflow that sets `design`, `designAgent`, `planPromptTemplate`, etc. as inputs to `code_parallel`) is updated to pass the renamed inputs (`openspecPlanAgent`, `openspecHumanApproval`, etc.) through unchanged in spirit — same knobs, new names reflecting the new sub-workflow.

## Risks / Trade-offs

- **[Risk] OpenSpec CLI must be installed/available wherever `coding-harness` workers run.** → Mitigation: add it to the harness's setup/Dockerfile the same way `git`/`gh` already are prerequisites for the existing git-wrapping taskdefs.
- **[Risk] Constraining `tasks.md` to strict file-disjoint groups may force the planning agent into fewer, coarser groups for changes with genuinely entangled files, reducing parallelism.** → Mitigation: this is the same constraint the old `plan` step already had ("no two sub-tasks may edit the same file") — no regression, just enforced deterministically instead of hoped-for.
- **[Risk] Reusing the AI-judge review path means judging the whole OpenSpec artifact set (proposal/specs/design/tasks) in one pass, a broader surface than today's `docs/design/*.md`-only judgment.** → Mitigation: same judge prompt pattern as today (read-only, structured `{approved, feedback}` output), generalized to read whichever artifact paths `openspec_status` reports for the change — no new judging mechanism, just a broader read set.

## Migration Plan

1. Add new taskdefs (`openspec_new_change.json`, `openspec_instructions.json`, `openspec_status.json`, `openspec_tasks_to_subtasks.json`) and their workers.
2. Add `openspec_plan.json` sub-workflow.
3. Update `code_parallel.json`: replace the `design_gate` SWITCH + `plan` SIMPLE task with an `openspec_plan` SUB_WORKFLOW task; repoint `build_forks`'s `result` input at `${openspec_plan.output.subtasks}`.
4. Update `issue_to_pr.json` (and any other direct callers of `code_parallel`) input parameters.
5. Delete `design_docs.json`, `defaults/prompts/design.md`, `defaults/prompts/plan.md`.
6. Update `coding-harness/README.md` and `SKILL.md`.
7. No data migration needed — these are workflow definitions re-registered with Conductor; existing in-flight runs on the old definitions complete against their original version.

## Open Questions

- Should `openspec_tasks_to_subtasks` be a small dedicated worker (Python, alongside the other `workers/`) or expressible as a `JSON_JQ_TRANSFORM` given `tasks.md` is markdown, not JSON? (Leaning dedicated worker, since JQ can't parse markdown headings/checkboxes robustly — captured as a task in tasks.md.)
- Does the target repo need its own `openspec/config.yaml` with the `tasks` rule pre-seeded, or should `openspec_new_change`/`openspec_plan` inject it if absent?
