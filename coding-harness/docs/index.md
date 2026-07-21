# Coding Harness

Autonomous coding as durable [Conductor](https://conductor-oss.org/) workflows. Point the harness
at a local repository, GitHub issue, or pull request and it can plan, implement in isolated git
worktrees, review, revise, and publish while every step remains observable and retryable.

## What it provides

- Parallel implementation with `FORK_JOIN_DYNAMIC` and one worktree per independent subtask.
- Durable `design_docs` and `code_subtask` sub-workflows.
- Claude, Codex, and Gemini coding backends, selectable per role.
- Human gates before design approval and remote publication.
- Structured token/cost accounting, worker progress, retries, and Conductor UI visibility.
- Task-definition-owned runtime deadlines, with no competing workflow or worker timeout.

## Workflow loop

```text
GitHub issue ──issue_to_pr──▶ pull request ──pr_review──▶ review feedback
                                  ▲                          │
                                  └────────address_pr────────┘

Local repository ──code_parallel──▶ planned parallel changes ──▶ merged branch
                 └─local_review──▶ read-only findings before commit
```

## Next steps

- [Quickstart](quickstart.md) — launch the local stack and run a first workflow.
- [Workflow guide](workflows.md) — choose a workflow, backend, design mode, and publication gate.
- [Workflow input reference](workflow-inputs.md) — required and optional parameters, including exact defaults.
- [Worker and backend reference](CODING_AGENT_WORKER.md) — inputs, guardrails, worker internals,
  prompt templates, and Git/GitHub operations.
- [Repository README](https://github.com/conductor-oss/conductor-agents/blob/main/coding-harness/README.md) — TUI setup and the complete documentation map.
- [Agent skill](https://github.com/conductor-oss/conductor-agents/blob/main/coding-harness/SKILL.md) — instructions for an AI assistant operating the harness.
