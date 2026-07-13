# Coding Agent

!!! note "🚧 Coming soon"
    This harness is on the roadmap and not yet published. Want to help build it — or add your own? See [Add your agent](../contributing.md).

An autonomous coding agent that **plans, edits, runs, and verifies** changes across a repository, running as a durable Conductor workflow.

## What it will do

- Break a task into a plan, then work it as a durable agent loop.
- Edit files, run builds and tests, read the results, and iterate until the change is verified.
- Spawn a sub-workflow per task so long jobs survive restarts and run in parallel where independent.

## Conductor features

| Feature | Role |
|---|---|
| `DO_WHILE` + `LLM_CHAT_COMPLETE` | The plan → edit → run → verify agent loop |
| Sub-workflows | One durable branch per task, composed into the run |
| Full execution history | Every edit, command, and model call replayable in the Conductor UI |

[← Back to catalog](../index.md){ .md-button }
