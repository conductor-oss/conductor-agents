## Why

`coding-harness`'s planning is currently two ad-hoc, non-deterministic LLM calls: `design_docs.json` lets one agent freely decide the shape/order of design docs under `docs/design/`, and `code_parallel.json`'s `plan` step asks a second agent to decompose the instruction into subtasks via a JSON schema, with no guarantee the subtasks are actually independent or file-disjoint. Both steps route around Conductor's own primitives instead of using them, which makes runs hard to reproduce, hard to resume, and prone to producing subtasks that collide when forked in parallel. OpenSpec already defines a structured, dependency-ordered artifact lifecycle (proposal → specs/design → tasks) with CLI commands (`openspec new change`, `openspec instructions`, `openspec status`) that map cleanly onto discrete Conductor tasks — replacing the freeform agent-driven planning with a deterministic, resumable Conductor workflow.

## What Changes

- **BREAKING**: Remove `design_docs.json` as the design step. `code_parallel.json`'s design gate is replaced by an `openspec_plan` sub-workflow that drives the `openspec` CLI directly and reuses `design_docs.json`'s existing human-or-AI-judge `DO_WHILE` review loop unchanged in mechanism, retargeted at the generated OpenSpec artifacts instead of `docs/design/*.md`.
- **BREAKING**: Remove the freeform `plan` step (structured-output LLM decomposition) from `code_parallel.json`. Subtask decomposition now comes from OpenSpec's `tasks.md`, parsed deterministically.
- Add Conductor task defs that wrap the `openspec` CLI as typed, deterministic steps (not agent-decided shell calls): create the change, fetch per-artifact instructions, and read status — the sequencing (proposal → specs/design → tasks) is driven by `openspec status`'s dependency graph, not by agent judgment.
- Keep an LLM agent for artifact *content* generation only (writing proposal.md/specs/design.md/tasks.md against the instructions the CLI returns) — content generation stays parallel-fork-friendly per artifact, but sequencing/gating is deterministic.
- Constrain `tasks.md` generation (via `openspec/config.yaml` per-artifact rules) so each task group is independent and file-disjoint, and annotates its target files and test command — the same shape `code_parallel.json` already required from its old `plan` step's structured output.
- Add a deterministic parser step that reads the generated `tasks.md` and produces the `subtasks[]` array (`id`, `description`, `files`, `testCmd`) that feeds the existing `FORK_JOIN_DYNAMIC` fan-out in `code_parallel.json`. `code_subtask.json` and `merge_worktrees` are unchanged.
- Update `coding-harness` prompts, README, and SKILL.md to describe the new OpenSpec-driven planning flow; retire `design.md`/`plan.md` prompt templates in favor of OpenSpec's own artifact instructions.

## Capabilities

### New Capabilities
- `harness-openspec-planning`: A Conductor sub-workflow that drives the `openspec` CLI (new change, per-artifact instructions, status) to deterministically produce proposal/specs/design/tasks artifacts for a `code_parallel` run, replacing `design_docs.json` and the old `plan` step's sequencing — reusing `design_docs.json`'s existing human-or-AI-judge review loop unchanged, just retargeted at these artifacts.
- `harness-task-decomposition`: Deterministic conversion of a generated `tasks.md` into the independent, file-disjoint `subtasks[]` array consumed by `code_parallel.json`'s existing `FORK_JOIN_DYNAMIC` fan-out.

### Modified Capabilities
(none — `openspec/specs/` is currently empty; there is no existing spec-level behavior to modify.)

## Impact

- `coding-harness/workers/workflows/design_docs.json` — removed, replaced by the new `openspec_plan` sub-workflow.
- `coding-harness/workers/workflows/code_parallel.json` — design gate and `plan` step replaced; `build_forks` now consumes the parsed OpenSpec `subtasks[]` instead of `plan.output.structured.subtasks`.
- `coding-harness/workers/workflows/code_subtask.json`, `merge_worktrees` — unchanged.
- `coding-harness/workers/workflows/taskdefs/` — new task defs for the `openspec` CLI wrapper steps (new change, instructions, status) and for the tasks.md → `subtasks[]` parser.
- `coding-harness/workers/defaults/prompts/design.md`, `plan.md` — retired; artifact content-generation prompts now derive from `openspec instructions <artifact>` output.
- `coding-harness/workers/workflows/issue_to_pr.json` — input parameters that reference the old design/plan phases (`designAgent`, `planPromptTemplate`, etc.) updated to the new OpenSpec-driven inputs.
- `coding-harness/README.md`, `coding-harness/SKILL.md` — updated to describe OpenSpec-driven planning; the human/AI-judge review loop itself is unchanged in mechanism.
- `coding-harness/tui/catalog.py` — its standalone `design_docs` `WorkflowSpec`/result-formatter/`LAUNCHABLE` entry is removed, and the `code_parallel`/`issue_to_pr`/`address_pr` `WorkflowSpec` field lists are updated to the renamed `openspec_plan` inputs.
- New `openspec/config.yaml` per-artifact rule for `tasks` (in any repo `code_parallel` targets) requiring independent, file-disjoint, annotated task groups.
