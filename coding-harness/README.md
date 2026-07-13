# Conductor Coding Harness

Autonomous coding as durable [Orkes/Conductor OSS](https://conductor-oss.org) workflows.
Point it at a repo, an issue, or a PR and it plans, writes, reviews, and revises code —
running coding agents (**Claude Agent SDK**, **OpenAI Codex**, or **Google Gemini**) in
parallel across isolated git worktrees, with every run observable, resumable, and retryable.

```
issue  ──issue_to_pr──▶  PR  ──pr_review──▶  review comments
                          ▲                        │
                          └──────address_pr────────┘   (revise from feedback)
```

## Start in 60 seconds

This path assumes Python 3.13+, the `conductor` CLI, Java 21+, `jq`, and one authenticated
coding backend. Claude is the default; run `claude login` or set
`ANTHROPIC_API_KEY`. See [Prerequisites](#prerequisites) for Codex, Gemini, and GitHub.

```bash
# Terminal 1 — from coding-harness/
./run.sh
```

`run.sh` starts a local Conductor server when needed, creates the worker environment,
registers/updates all definitions, runs the SIMPLE-task worker gate, and starts the workers.

In a second terminal, point the harness at any local checkout:

```bash
export CONDUCTOR_SERVER_URL=http://localhost:8080/api
conductor workflow start --workflow code_parallel --input \
  '{"repoPath":"/absolute/path/to/repo","instruction":"Add a health endpoint and tests","design":false}'
```

The command returns a workflow ID. Watch it with
`conductor workflow get-execution <workflowId> -c`, or open the Conductor UI at
<http://localhost:8080>. Registration is safe to rerun after definitions change.

For a remote server, set `CONDUCTOR_SERVER_URL` before `./run.sh`; the script will never
replace an unreachable remote with a local server.

## Workflows

| Workflow | Does | Key inputs |
|---|---|---|
| **`issue_to_pr`** | GitHub issue → pull request (resolves it, opens a PR that closes the issue) | `repo`, `issueNumber` |
| **`pr_review`** | Reviews a PR and posts a formal review (inline comments + verdict; never approves) | `repo`, `prNumber` |
| **`address_pr`** | Applies a PR's review feedback and updates the same branch | `repo`, `prNumber` |
| **`code_parallel`** | Codes a multi-part change in a local repo (decompose → parallel → merge) — the coding core the others wrap | `repoPath`, `instruction` |
| **`github_demo`** | Minimal clone → change → PR (GitHub connectivity smoke test) | `repoUrl`, `instruction` |

Coding runs on any backend per task (`claude` default, `codex`, `gemini` — or inferred from
the model id), mixable within a run. Full input tables, examples, and prerequisites:
**[`workers/README.md`](workers/README.md)**.

## Terminal UI

The optional terminal interface opens into a **chat** where an agent
(default sonnet) drives the harness for you ("review PR 7 on acme/app", "how many runs
failed?"); forms and a live dashboard are a slash-command away:

```bash
cd tui && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cd ..
# run from the repo root (ANTHROPIC_API_KEY needed for chat):
CONDUCTOR_SERVER_URL=http://localhost:8080/api tui/.venv/bin/python -m tui        # chat
CONDUCTOR_SERVER_URL=http://localhost:8080/api tui/.venv/bin/python -m tui --dashboard  # forms
```

Ask chat to “register/update the workflows”, use `/register`, or press `g` on the dashboard
whenever definitions change. The TUI confirms the target server, updates the definitions, and
runs the SIMPLE-task worker gate before reporting success. Chat starts at most one workflow per
user message; when the requested action is ambiguous, it asks which workflow you want first.
Before any `code_parallel` path starts, chat also asks whether you want design docs. Choosing
design enables an iterative human review gate by default: approve to continue to coding, or give
feedback for another design pass. You can opt into an automated read-only judge instead; its
review and loop limits default to 5 and are configurable.

## How it works

```text
Conductor workflow
  ├─ durable lifecycle, retries, fan-out, human review gates
  ├─ coding_agent worker → Claude | Codex | Gemini
  ├─ one isolated git worktree per parallel subtask
  └─ gitops workers → commit, merge, push, GitHub PR/review operations
```

`code_parallel` is the coding core. It asks a read-only agent to decompose the goal,
creates a dynamic Conductor fork, implements each independent slice in its own worktree,
then merges the branches and aggregates files, tokens, and cost. The GitHub workflows wrap
that core with issue, PR, review, and push operations.

## Documentation

| Doc | For |
|---|---|
| [`workers/README.md`](workers/README.md) | **User guide** — install, run, the full workflow catalog with inputs & examples. |
| [`tui/README.md`](tui/README.md) | **Terminal UI** — the interactive interface: launch runs, watch agents live, manage them. |
| [`SKILL.md`](SKILL.md) | **Agents** — how a Claude Code / LLM agent drives the harness (when to use which workflow, how to trigger, gotchas). |
| [`docs/CODING_AGENT_WORKER.md`](docs/CODING_AGENT_WORKER.md) | **Reference** — the `coding_agent` worker, all workflows, backends, guardrails, remote git/GitHub. |
| [`docs/CLAUDE_AGENT_SDK.md`](docs/CLAUDE_AGENT_SDK.md) | Claude Agent SDK deep-dive (features, interception, gotchas). |
| [`docs/SPEC.md`](docs/SPEC.md) · [`docs/DESIGN.md`](docs/DESIGN.md) | **Historical** — the original spec-driven design, since superseded. |

## Prerequisites

- Python 3.13+, `jq`, and the `conductor` CLI.
- A reachable Conductor server. For local development: `conductor server start` (Java 21+).
- At least one authenticated backend:
  - Claude: `claude login` or `ANTHROPIC_API_KEY`.
  - Codex: `~/.codex/auth.json` or `OPENAI_API_KEY`.
  - Gemini: Gemini CLI plus `GEMINI_API_KEY`.
- For GitHub workflows: authenticated `gh` CLI (`gh auth login` or a valid `GH_TOKEN`).
- The target repository's own build/test toolchain.

Full setup details and every workflow input are in
[`workers/README.md`](workers/README.md#prerequisites).
