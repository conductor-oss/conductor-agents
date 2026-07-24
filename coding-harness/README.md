# Conductor Coding Harness

Autonomous coding as durable [Orkes/Conductor OSS](https://conductor-oss.org) workflows.
Point it at a repo, an issue, or a PR and it plans, writes, reviews, and revises code —
running coding agents (**Claude Agent SDK**, **OpenAI Codex**, or **Google Gemini**) in
parallel across isolated git worktrees, with every run observable, resumable, and retryable.

```
issue  ──issue_to_pr──▶  PR  ──pr_review──▶  review comments
                          ▲                        │
                          └──────address_pr────────┘   (revise from feedback)

local checkout ──local_review──▶ read-only findings before commit
```

## Start in 60 seconds

This path assumes Python 3.13+, Node 20.19+, the `conductor` CLI, Java 21+, `jq`, and one authenticated
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
  '{"repoPath":"/absolute/path/to/repo","instruction":"Add a health endpoint and tests"}'
```

The checkout is used as a source repository. The workflow creates a persistent
`.cc-worktrees/run-<workflow-id>` workspace from committed `HEAD`, so it never switches the
source folder's branch or copies its uncommitted changes.

The command returns a workflow ID. Watch it with
`conductor workflow get-execution <workflowId> -c`, or open the Conductor UI at
<http://localhost:8080>. Registration is safe to rerun after definitions change.

Set `CONDUCTOR_SERVER_URL` before `./run.sh` for a remote or authenticated server. The bootstrap
starts local OSS only when an unauthenticated localhost endpoint is genuinely unreachable. Any
HTTP response (including 401/403), configured Conductor credentials, or
`CONDUCTOR_SERVER_TYPE=Enterprise` prevents OSS fallback and surfaces the connection/auth error.

Authenticated Python workers and the TUI read `CONDUCTOR_AUTH_KEY` and
`CONDUCTOR_AUTH_SECRET` from their environment. Workers pass the pair explicitly to the
Conductor SDK; the TUI exchanges it for an API token and authenticates all REST and registration
calls. Set both variables together; a partial pair fails fast without printing either value.
The same connection loader is used by `./run.sh`, `workers/register.sh`, and
`workers/run_workers.sh`; explicit environment values take precedence over `.env` defaults.

## Workflows

| Workflow | Does | Key inputs |
|---|---|---|
| **`issue_to_pr`** | GitHub issue → pull request (resolves it, opens a PR that closes the issue) | `repo`, `issueNumber`, optional `base`, `design`, `planAgent`, `codeAgent` |
| **`pr_review`** | Reviews a PR and posts a formal review (inline comments + verdict; never approves) | `repo`, `prNumber`, optional `agent`, `model`, `approve` |
| **`local_review`** | Reviews a local checkout against a freshly fetched remote branch; never writes, commits, pushes, or posts | `repoPath`, optional `baseRemote`, `baseBranch` |
| **`address_pr`** | Applies a PR's review feedback and updates the same branch | `repo`, `prNumber`, optional `engine`, `agent`, `model`, `design` |
| **`code_parallel`** | Codes a multi-part change in a local repo (decompose → parallel → merge) — the coding core the others wrap | `repoPath`, `instruction` |
| **`feature_campaign`** | Checkpoint-first design → DAG → resumable waves → real-system verification; leaves a local branch and never publishes | `repoPath`, `instruction` |
| **`openspec_development`** | Apply-ready OpenSpec change → complexity routing → implementation → verification → archive | `repoPath`, `specSource`, `changeId` |
| **`github_demo`** | Minimal clone → change → PR (GitHub connectivity smoke test) | `repoUrl`, `instruction` |

Coding runs on any backend per task (`claude` default, `codex`, `gemini` — or inferred from
the model id), mixable within a run. The complete required/optional input contract and defaults
are in **[`docs/workflow-inputs.md`](docs/workflow-inputs.md)**; examples and prerequisites are
in **[`workers/README.md`](workers/README.md)**.

Choose a workflow profile with `modelProfile`, manage reusable agent guidance with prompt
templates, and use the local-source OpenSpec mode when the checked-out spec repository is also
the implementation repository. These are documented in [Models and profiles](docs/model-profiles.md),
[Prompt templates](docs/templates.md), and [Local OpenSpec development](docs/openspec.md).

Runtime deadlines are owned exclusively by Conductor task definitions. Workflow inputs and
workers do not impose a second agent wall-clock timeout; adjust `responseTimeoutSeconds`,
`timeoutSeconds`, and `timeoutPolicy` on the applicable task definition instead.

## Terminal UI

The optional terminal interface opens into a **chat** where an agent
(default sonnet) drives the harness for you ("review PR 7 on acme/app", "how many runs
failed?"); forms and a live dashboard are a slash-command away:

```bash
cd tui && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cd ..
# run from coding-harness/ (ANTHROPIC_API_KEY needed for chat):
CONDUCTOR_SERVER_URL=http://localhost:8080/api tui/.venv/bin/python -m tui        # chat
CONDUCTOR_SERVER_URL=http://localhost:8080/api tui/.venv/bin/python -m tui --dashboard  # forms
```

Ask chat to “register/update the workflows”, use `/register`, or press `g` on the dashboard
whenever definitions change. The TUI confirms the target server, updates the definitions, and
runs the SIMPLE-task worker gate before reporting success. Chat starts at most one workflow per
user message; when the requested action is ambiguous, it asks which workflow you want first.
Every `code_parallel` path always plans through OpenSpec first (proposal/specs/design/tasks) —
there's no "skip planning" toggle. That plan goes through an iterative human review gate by
default: approve to continue to coding, or give feedback for another planning pass. You can opt
into an automated structured judge instead; its read-only review uses the `coding_agent` worker.
The plan loop is capped at five passes by default and can be raised with `openspecMaxIterations`.
Before a form, chat, or schedule launch, the TUI resolves every applicable prompt role from the
local template library and records its source in the workflow input; unmatched roles fall through
to repo `.conductor/<key>.md` files and bundled defaults in the worker.
You can include a local checkout in chat, for example: “review PR 42 on acme/app using
`~/src/app`”. The TUI expands the path, previews the isolated worktree, and warns when source
changes will be ignored.
For a pre-commit review of the files already in your checkout, ask chat to “review my local
changes in `~/src/app`” or choose **Review local changes**. That starts `local_review`, which
uses that directory directly and compares staged, unstaged, untracked, and locally committed-ahead
changes to `origin/main` by default. It does not alter the checkout or post anything to GitHub.

## How it works

```text
Conductor workflow
  ├─ durable lifecycle, retries, fan-out, human review gates
  ├─ coding_agent worker → Claude | Codex | Gemini
  ├─ one isolated git worktree per parallel subtask
  └─ gitops workers → commit, merge, push, GitHub PR/review operations
```

`code_parallel` is the coding core. Its `openspec_plan` sub-workflow drives the `openspec` CLI
through a proposal/specs/design/tasks change — reviewed via the same human-or-AI-judge loop —
and deterministically parses the generated `tasks.md` into independent sub-tasks; `code_parallel`
then creates a dynamic Conductor fork, implements each independent slice in its own worktree,
merges the branches, and aggregates files, tokens, and cost. The GitHub workflows wrap that core
with issue, PR, review, and push operations.

For larger work, `feature_campaign` adds durable WAIT checkpoints after design, plan,
each integrated wave, and final verification. Its agents resume by session ID, DAG tasks
run only when dependencies are complete and their planned files are disjoint, and checks
come from `.conductor-code/checks.json` version 2. A campaign reports cumulative usage but
does not impose a campaign-wide spend cap, push, or open a PR.

`openspec_development` accepts an OpenSpec project/change from the target repo, another local
checkout, a Git remote, or a public HTTPS zip/tar bundle. It validates the change with the pinned
OpenSpec CLI, derives a repository-aware DAG, and selects `code_parallel` only for a small,
dependency-free, file-disjoint wave; other work goes through `feature_campaign`. After checks and
requirement-level verification it completes `tasks.md` and archives the change. Set
`useSpecSourceWorkspace:true` for a checked-out local source to make its repository the isolated
implementation worktree; it is then committed and opened as a draft PR. Other same-repo specs
remain on the verified local branch; external GitHub specs are archived on a separate branch and
opened as a draft PR. It never accepts credential values in workflow inputs.

## Documentation

| Doc | For |
|---|---|
| [`workers/README.md`](workers/README.md) | **User guide** — install, run, the full workflow catalog with inputs & examples. |
| [`tui/README.md`](tui/README.md) | **Terminal UI** — the interactive interface: launch runs, watch agents live, manage them. |
| [`SKILL.md`](SKILL.md) | **Agents** — how a Claude Code / LLM agent drives the harness (when to use which workflow, how to trigger, gotchas). |
| [`docs/index.md`](docs/index.md) | **Documentation site** — overview, quickstart, workflow selection, and reference navigation. |
| [`docs/model-profiles.md`](docs/model-profiles.md) | **Models** — profiles, provider catalog, policy precedence, costs, and TUI selection. |
| [`docs/templates.md`](docs/templates.md) | **Prompts** — template sources, scoping, placeholders, provenance, and trust boundaries. |
| [`docs/openspec.md`](docs/openspec.md) | **OpenSpec** — local/Git/archive sources, isolated worktrees, verification, archive, and PR behavior. |
| [`docs/CODING_AGENT_WORKER.md`](docs/CODING_AGENT_WORKER.md) | **Reference** — the `coding_agent` worker, all workflows, backends, guardrails, remote git/GitHub. |
| [`docs/CLAUDE_AGENT_SDK.md`](docs/CLAUDE_AGENT_SDK.md) | Claude Agent SDK deep-dive (features, interception, gotchas). |
| [`docs/SPEC.md`](docs/SPEC.md) · [`docs/DESIGN.md`](docs/DESIGN.md) | **Historical** — original design material, not the current operator contract. |

## Prerequisites

- Python 3.13+, Node.js 20.19+, npm, `jq`, and the `conductor` CLI.
- The `openspec` CLI: `openspec_plan`/`openspecops` shell out to a copy installed wherever workers
  run (on `PATH`); `openspec_development` uses its own pinned `@fission-ai/openspec` 1.6.0 worker
  dependency, which the harness installs locally via npm.
- A reachable Conductor server. For local development: `conductor server start` (Java 21+, SQLite
  — no other services needed). If a parallel-heavy run (`code_parallel`, `openspec_plan`) fails
  with `NonTransientException: [SQLITE_BUSY...]`, switch to the opt-in Postgres-backed server
  instead: set `CONDUCTOR_BACKEND=postgres` in `.env` and rerun `./run.sh` — it brings up
  [`docker-compose.postgres.yml`](docker-compose.postgres.yml) instead (requires Docker). Stop
  any already-running SQLite server first (`conductor server stop`) — a leftover process can
  keep answering on port 8080 and silently shadow the new container.
- At least one authenticated backend:
  - Claude: `claude login` or `ANTHROPIC_API_KEY`.
  - Codex: `~/.codex/auth.json` or `OPENAI_API_KEY`.
  - Gemini: Gemini CLI plus `GEMINI_API_KEY`.
- For GitHub workflows: authenticated `gh` CLI (`gh auth login` or a valid `GH_TOKEN`).
- The target repository's own build/test toolchain.

Copy [`.env.example`](.env.example) to `.env` for the full, documented list of
configuration variables (Conductor connection, backend keys, GitHub, logging,
tuning). `./run.sh` auto-loads `.env`; `.env` is gitignored.

Full setup details and every workflow input are in
[`workers/README.md`](workers/README.md#prerequisites).

## Scheduled GitHub automations

`issue_resolution_sweep`, `pr_review_sweep`, and `pr_address_sweep` claim eligible GitHub
revisions with trusted hidden comments and asynchronously start long-running child workflows.
Their dispatch inputs default to human approval. New PR commits and changed feedback create new
revisions; completed revisions are not repeated. See
[the workflow input reference](docs/workflow-inputs.md) for the exact sweep and dispatch fields.

Open **Automations** from the dashboard (`s`) for schedule CRUD/run-now. The default is
`0 */10 * ? * *` in the local timezone with catch-up disabled. Open the global **Approval
Inbox** with `a`; its five-second app-wide poll includes nested checkpoints and excludes timed
WAIT tasks. Credentials remain in environment/auth configuration, never schedule input.
Prompt overrides are ordinary schedule workflow inputs: use inline `*PromptTemplate` text or an
`@repo/path` reference. The worker records the actual resolved source and hash in each child
execution, while TUI library selections retain their user-file origin in `*PromptTemplateSource`.
Registration does not create schedules. See
[`docs/config/automation-schedule.example.json`](docs/config/automation-schedule.example.json).
