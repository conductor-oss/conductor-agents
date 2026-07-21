# Conductor Coding Harness

Scheduled GitHub loops use `pr_review_sweep`, `pr_address_sweep`, and
`issue_resolution_sweep`. Scans dynamically dispatch bounded candidates through
`automation_dispatch`; long approval gates never hold the next sweep open. Hidden GitHub markers
record revision claims and child IDs. Register with `./register.sh`, restart workers so the
`automation` module polls, then create schedules explicitly (registration creates none). The
exact sweep and dispatch inputs are listed in [`../docs/workflow-inputs.md`](../docs/workflow-inputs.md).

Autonomous coding as durable [Conductor](https://conductor-oss.org) workflows. Point it at
a repo, an issue, or a PR and it plans, writes, reviews, and revises code — running coding
agents (Claude Agent SDK, OpenAI Codex, or Google Gemini) in parallel across isolated git
worktrees, with every run observable, resumable, and retryable in Conductor.

**The PR lifecycle, as workflows:**

```
issue  ──issue_to_pr──▶  PR  ──pr_review──▶  review comments
                          ▲                        │
                          └──────address_pr────────┘   (revise from feedback)
```

- **`code_parallel`** is the coding core: decompose one instruction → code the parts in
  parallel → merge. The GitHub workflows wrap it with clone / push / PR plumbing.
- **`feature_campaign`** is the interactive path for complex work: design and DAG review,
  resumable dependency waves, profile-driven checks, and final verification on a local branch.
- **`openspec_development`** validates an apply-ready OpenSpec change, selects the appropriate
  coding path, verifies the result, and completes/archives the spec lifecycle.
- **Backends** are per-task: `claude` (default), `codex`, or `gemini` — or inferred from the
  model id. Mix them (plan on Claude, code on Codex, etc.).

---

## Start in 60 seconds

Assumes Python 3.13+, Node.js 20.19+, npm, `jq`, a reachable Conductor server, and at least one authenticated backend.
Run from the repository's `coding-harness/` directory:

```bash
# 1. Install
python3 -m venv workers/.venv
workers/.venv/bin/pip install -q -r workers/requirements.txt
npm install --no-audit --no-fund --prefix workers/openspec

# 2. Register task + workflow definitions on your Conductor server (idempotent)
export CONDUCTOR_SERVER_URL=http://localhost:8080/api
./workers/register.sh

# 3. Start the workers (they poll Conductor for work)
workers/.venv/bin/python workers/main.py
```

In another terminal, trigger a workflow:

```bash
export CONDUCTOR_SERVER_URL=http://localhost:8080/api
conductor workflow start --workflow issue_to_pr -i '{
  "repo": "https://github.com/you/your-repo.git",
  "issueNumber": 42
}'
```

If no server is running, start one first with `conductor server start` (Java 21+).
After definitions change, rerun `./workers/register.sh`, ask TUI chat to register them, use `/register`, or
`g` on the dashboard. Registration updates definitions and verifies the SIMPLE-task worker gate.

Watch progress in the Conductor UI or with `conductor workflow status <id>` — the coding
tasks push live per-turn updates (files touched, commands run, tokens). For an interactive
experience, use the **terminal UI** (`../tui/`): `python -m tui` to launch runs from a form
and watch agents work live — see [`../tui/README.md`](../tui/README.md).

### Prerequisites

- **Conductor server** reachable (`CONDUCTOR_SERVER_URL`, default `http://localhost:8080/api`).
  For authenticated servers, set both `CONDUCTOR_AUTH_KEY` and `CONDUCTOR_AUTH_SECRET`; the
  Python workers pass them directly to the SDK and reject a partial pair.
- **At least one agent backend**, authenticated in the worker's environment:
  | Backend | `agent` value | Auth |
  |---|---|---|
  | Claude Agent SDK (default) | `claude` | `claude login` or `ANTHROPIC_API_KEY` |
  | OpenAI Codex | `codex` | bundled `openai-codex` SDK — reuses `~/.codex/auth.json` / `OPENAI_API_KEY` (`CODEX_DRIVER=cli` uses the `codex` CLI) |
  | Google Gemini | `gemini` | `npm i -g @google/gemini-cli`, `GEMINI_API_KEY` (or `~/.gemini/.env`) |
- **For the GitHub workflows** (`issue_to_pr`, `pr_review`, `address_pr`, `github_demo`): the
  [`gh` CLI](https://cli.github.com) installed and authenticated (`gh auth login`, or
  `GH_TOKEN` in the worker's env). The first remote task runs `gh auth setup-git` so plain
  git-over-HTTPS uses gh's credentials — no tokens in URLs.
- The **target toolchain** for whatever the agents build (node, go, etc.).

Every configuration variable — Conductor connection, backend keys, GitHub, logging,
and tuning knobs — is documented in [`../.env.example`](../.env.example). Copy it to
`.env` (gitignored); `../run.sh` auto-loads it, or `set -a; . ../.env; set +a` before
running `main.py` / `run_workers.sh` directly.

---

## Workflows

Seven user-facing workflows plus three internal sub-workflows. All inputs are JSON passed with
`conductor workflow start --workflow <name> -i '{...}'`. Only the inputs marked **required**
must be set; the rest have the defaults shown.

### `openspec_development` — implement and archive an OpenSpec change

Use this when an apply-ready OpenSpec change is the source of truth. The source can be local,
a Git remote, or a public HTTPS archive. The workflow validates and snapshots the change,
assesses a repository-aware DAG, selects `code_parallel` or `feature_campaign`, runs final
checks plus requirement-level verification, then completes `tasks.md` and archives the change.

| Input | Default | Meaning |
|---|---|---|
| `specSource` / `changeId` | **required** | OpenSpec source and kebab-case change ID. |
| `repoPath` | `""` | Target repo; required unless the local source-workspace option is enabled. |
| `useSpecSourceWorkspace` | `false` | Use an absolute local checked-out source as the implementation worktree; verified runs push a draft PR. |
| `specSourceType` | `auto` | `auto`, `local`, `git`, or `url`. |
| `specRef` / `specPath` | `""` | Optional Git ref and path to the OpenSpec project. |
| `specWritebackRepo` | `""` | Required for URL sources; receives the archived change as a draft PR. |
| `executionMode` | `auto` | Deterministic complexity routing, or explicit `parallel` / `campaign`. |
| `maxTasks` / `maxParallelism` / `maxWaves` | `25` / `6` / `20` | DAG and execution bounds. |
| `checksConfig` / `finalProfile` | `.conductor-code/checks.json` / `""` | Final verification profile. |

```bash
conductor workflow start --workflow openspec_development -i '{
  "repoPath": "/path/to/repo",
  "specSource": ".",
  "changeId": "add-health-endpoint"
}'
```

With `useSpecSourceWorkspace:true`, only the selected OpenSpec tree is materialized in an owned
worktree; the original checkout is untouched, and ignored OpenSpec artifacts are force-staged only
for the lifecycle commit. Same-repo specs otherwise archive on the verified local implementation
branch. External GitHub specs use an archive branch and draft PR. Credentials come from the worker environment and authenticated
`gh`; never place tokens in `specSource` or any workflow input. This v1 workflow accepts only
apply-ready changes—it does not author proposals.

### `feature_campaign` — interactive complex feature work

Use this instead of `code_parallel` when design and plan need iterative approval, implementation
has dependencies, or real-system checks need operator-controlled environments. It pauses after
every design pass, approved DAG, integrated wave, attached-server run, and final verification.
Agents resume the same session/worktree after feedback or budget exhaustion.

| Input | Default | Meaning |
|---|---|---|
| `repoPath` / `instruction` | **required** | Local repository and feature goal. |
| `changeBranch` | derived | `feature-campaign/<workflow-id>` when blank. |
| `designDir` | `docs/design` | Design artifact directory. |
| `*Agent` / `*Model` | `claude` / `""` | Design, plan, code, and review backends/models. |
| `maxTurns` / `maxBudgetUsd` | `500` / `50.0` | Per invocation; no aggregate spend cap. |
| `maxTasks` / `maxParallelism` / `maxWaves` | `25` / `6` / `20` | Validated DAG and wave bounds. |
| `designMaxRevisions` / `planMaxRevisions` | `5` / `5` | Review-loop bounds. |
| `checksConfig` | `.conductor-code/checks.json` | Version-2 named check profiles. |

Checkpoint actions are Continue, Revise, Adopt edits, Run checks, Set profiles, Stop, and Later.
Blocking checks and integration conflicts fail soft and return to the checkpoint. Stop retains
the branch with an incomplete outcome. Campaigns never push or open a PR.

```bash
conductor workflow start --workflow feature_campaign -i '{
  "repoPath": "/path/to/repo",
  "instruction": "Add a durable event subsystem with migrations and tests"
}'
```

### `code_parallel` — code a change, in parallel

Decompose one instruction into independent sub-tasks, code each on its own git worktree/branch
in parallel, then merge back into a change branch. Optional up-front design-docs phase. Works
from a **local source checkout** (`repoPath`); it doesn't clone or push (the GitHub workflows
do that). The source checkout is never switched or edited: the run uses
`.cc-worktrees/run-<workflow-id>` from committed `HEAD`.

| Input | Default | Meaning |
|---|---|---|
| `repoPath` | **required** | Local directory to work in. Need not be a git repo — it's initialized if needed. |
| `instruction` | **required** | The coding goal to decompose and implement. |
| `changeBranch` | `code-parallel` | Branch the parallel work merges into. |
| `design` | `false` | Explicit choice: if true, generate and approve design docs before coding. The TUI always asks. |
| `designHumanApproval` | `true` | Pause after each design pass for approval or actionable feedback. False uses the read-only `coding_agent` judge. |
| `designMaxIterations` | `5` | Maximum design/review passes before the workflow fails closed; may be raised. |
| `maxSubtasks` | `6` | Upper bound on the parallel fan-out. |
| `planAgent` / `codeAgent` / `designAgent` | `claude` | Backend for the planner / coders / design step. |
| `planModel` / `codeModel` / `designModel` | `""` | Model id; empty = the backend's default. |
| `maxTurns` / `maxBudgetUsd` | `500` / `50.0` | Per-agent turn and spend caps. The planner also defaults to 500 turns. Runtime timeouts come from the Conductor task definition. |

```bash
conductor workflow start --workflow code_parallel -i '{
  "repoPath": "/path/to/repo",
  "instruction": "Add a REST API with CRUD endpoints for notes, plus tests.",
  "changeBranch": "notes-api",
  "design": true,
  "maxSubtasks": 4,
  "planAgent": "claude",
  "codeAgent": "codex"
}'
```

**Output:** `changeBranch`, `subtasks`, `merged`, `conflicts`, **`totalTokens`**,
**`totalCostUsd`**, and a `summary` with a per-sub-task + `{plan, design, subtasks, merge}`
token/cost breakdown.

### `issue_to_pr` — GitHub issue → pull request

Fetch an issue, prepare an isolated workspace, resolve it with `code_parallel`, push a branch,
and open a PR whose body closes the issue. The workflow clones its own temporary source checkout.

| Input | Default | Meaning |
|---|---|---|
| `repo` | **required** | Repo URL or `owner/name`. |
| `issueNumber` | **required** | Issue to resolve. |
| `base` | `main` | Base branch for the PR. |
| `design` | `false` | Explicit choice: generate and approve design docs before coding. The TUI always asks. |
| `designHumanApproval` | `true` | Human review each pass; false selects the automated read-only judge. |
| `designMaxIterations` | `5` | Maximum design/review passes before the workflow fails closed. |
| `maxSubtasks` | `4` | Parallel fan-out cap. |
| `planAgent` / `codeAgent` / `designAgent` | `claude` | Backends. |
| `maxTurns` / `maxBudgetUsd` | `300` / `50.0` | Per-agent turn and spend caps. Runtime timeouts come from the Conductor task definition. |

```bash
conductor workflow start --workflow issue_to_pr -i '{
  "repo": "https://github.com/you/your-repo.git",
  "issueNumber": 42,
  "base": "main",
  "codeAgent": "claude"
}'
```

**Output:** `prNumber`, `prUrl`, `changeBranch`, `subtasks`, `totalTokens`, `totalCostUsd`.

### `pr_review` — review a PR, post comments

Read a PR's diff (plus surrounding code for context), produce a structured review with a
**read-only** agent, and post it as a formal GitHub review: inline file/line comments + a
summary + a verdict. Verdict is `COMMENT`, or `REQUEST_CHANGES` when a blocking issue is
found — **never `APPROVE`** (a bot approval could satisfy branch protection). Read-only: it
can only comment, never modify the PR.

| Input | Default | Meaning |
|---|---|---|
| `repo` | **required** | Repo URL or `owner/name`. |
| `prNumber` | **required** | PR to review. |
| `agent` | `claude` | Backend for the reviewer. |
| `model` | `""` | Model id; empty = backend default. |
| `approve` | `false` | Open a signalable publication gate before posting the review. |
| `reviewPromptTemplate` / `reviewPromptTemplateSource` | `""` / `""` | Optional reviewer-template override and its provenance. |
| `maxTurns` / `maxBudgetUsd` | `250` / `50.0` | Turn and spend caps. Runtime timeouts come from the Conductor task definition. |

```bash
conductor workflow start --workflow pr_review -i '{
  "repo": "https://github.com/you/your-repo.git",
  "prNumber": 7
}'
```

**Output:** `event` (COMMENT/REQUEST_CHANGES), `inlineCount`, `reviewUrl`, `changedFiles`,
`tokenUsed`, `costUsd`.

### `local_review` — review a local checkout before committing

Review an existing checked-out repository against a freshly fetched remote baseline without
changing it. Unlike the implementation workflows, this intentionally uses `repoPath` directly so
the review includes local commits ahead of the remote, staged and unstaged edits, and untracked
files. It never creates a worktree, edits, stages, commits, pushes, or posts to GitHub; the agent
gets only `Read`, `Grep`, and `Glob`.

| Input | Default | Meaning |
|---|---|---|
| `repoPath` | **required** | Local checked-out Git repository on the worker host. |
| `baseRemote` | `origin` | Configured remote to refresh before comparison. |
| `baseBranch` | `main` | Remote branch used as the baseline. |
| `agent` / `model` | `claude` / `""` | Read-only reviewer backend and optional model id. |
| `maxTurns` / `maxBudgetUsd` | `250` / `50.0` | Per-agent limits. |

```bash
conductor workflow start --workflow local_review -i '{
  "repoPath":"/absolute/path/to/repo",
  "baseRemote":"origin",
  "baseBranch":"main"
}'
```

**Output:** `summary`, `verdict`, `comments`, `changedFiles`, `baseRef`, `tokenUsed`, `costUsd`.

### `address_pr` — revise a PR from its feedback

Consolidate a PR's review feedback (conversation comments + reviews + inline threads, skipping
the harness's own), check out the PR branch, make the changes, and push to the **same branch**
(updating the PR — no new PR). Safely re-runnable: the harness's own replies are tagged and
skipped, and it no-ops when there's no outstanding feedback.

| Input | Default | Meaning |
|---|---|---|
| `repo` | **required** | Repo URL or `owner/name`. |
| `prNumber` | **required** | PR whose feedback to address. |
| `engine` | `code_parallel` | How to code: `code_parallel` (decompose+parallel) or `coding_agent` (single session, cheaper for small feedback). |
| `agent` | `claude` | Backend. |
| `design` / `designHumanApproval` / `designMaxIterations` | `false` / `true` / `5` | Optional design phase and its bounded approval settings. |
| `fixPromptTemplate` / `fixPromptTemplateSource` | `""` / `""` | Optional fix-template override and its provenance. |
| `maxSubtasks` / `maxTurns` / `maxBudgetUsd` | `4` / `250` / `50.0` | Parallelism, turn, and spend caps (`maxSubtasks` is used only by the `code_parallel` engine). Runtime timeouts come from the Conductor task definition. |

```bash
conductor workflow start --workflow address_pr -i '{
  "repo": "https://github.com/you/your-repo.git",
  "prNumber": 7,
  "engine": "coding_agent"
}'
```

**Output:** `head`, `engine`, `commentCount`, `pushed`, `replyUrl`.

### `github_demo` — minimal clone → change → PR

A small demo of the remote plumbing without `code_parallel`: clone, branch, one `coding_agent`
edit, commit, push, open a PR. Good for smoke-testing GitHub connectivity.

| Input | Default | Meaning |
|---|---|---|
| `repoUrl` | **required** | Repo to clone. |
| `instruction` | **required** | The change to make. |
| `changeBranch` | `conductor-harness-change` | Branch to push. |
| `base` | `""` | PR base (empty = repo default). |
| `prTitle` | `""` | PR title (empty = derived from the commit via `gh --fill`). |
| `agent` / `model` | `claude` / `""` | Backend + model. |

### Internal sub-workflows

- **`design_docs`** — iteratively writes a consistent set of design docs under `docs/design/`,
  reviews each pass, and commits only an approved design. Human review is the default: approve to
  exit, or submit feedback that drives the next pass. With `humanApproval:false`, a read-only
  `coding_agent` judge reads and reviews the design documents instead. `designMaxIterations` defaults to
  5 and can be raised. Invoked by `code_parallel` when `design:true`; also runnable standalone.
- **`code_subtask`** — one parallel unit of `code_parallel` (`worktree_add → coding_agent →
  commit`). Driven by the dynamic fork; not called directly.
- **`campaign_subtask`** — one resumable, file-scoped DAG task for `feature_campaign`.

---

## Prompt templates (custom instructions)

Every workflow ships a tuned built-in prompt, but you can fully override an agent step's prompt
with your own instructions — from three layers, highest precedence first:

1. **Explicit input** — a `*PromptTemplate` workflow input (`localReviewPromptTemplate`, `reviewPromptTemplate`,
   `codePromptTemplate`, `planPromptTemplate`, `designPromptTemplate`, `fixPromptTemplate`,
   phase-specific campaign/OpenSpec templates, and approval/design judge templates);
   inline text, or `@repo/path` to read the prompt from a file in the checkout.
2. **Repo-resident** — a `.conductor/<key>.md` file committed in the target repo
   (`local_review` · `pr_review` · `code` · `plan` · `design` · `address_pr`), read from the checkout. Applies to
   every run on that repo with **no payload change** — the natural fit for scheduled/CI automation.
3. **Shipped default** — the canonical built-in prompt in `defaults/prompts/<key>.md` (what the
   worker uses by default; the TUI seeds new templates from the same files).

Each template input has a paired `*PromptTemplateSource`. The coding worker returns the actual
`resolvedSource`, `templateKey`, and prompt `sha256` in `output.promptTemplate`; the requested
source is descriptive provenance and never overrides the resolver's security checks.

`{{diff}}` / `{{feedback}}` / `{{instruction}}` / `{{subtask}}` placeholders are filled with
runtime context; unused context is appended automatically. The output schema stays enforced
(a custom `pr_review` template still produces a structured review). Details + the full table:
[`../docs/CODING_AGENT_WORKER.md`](../docs/CODING_AGENT_WORKER.md) §14. Untrusted repos: set
`CODING_AGENT_REPO_TEMPLATES=0` in the worker env to disable the repo-file layer.

### Event-triggered review in CI (example)

Because the repo carries its own context — an `AGENTS.md` guide (auto-read into every agent's
prompt: how to build/test/review) and optionally a `.conductor/pr_review.md` or
`.conductor/local_review.md` prompt — a GitHub or local review workflow
Action only needs to start the workflow, no prompt in the payload. `CONDUCTOR_SERVER_URL` must
reach your Conductor server and the workers must be running (self-hosted or a hosted/Orkes
cluster):

```yaml
# .github/workflows/harness-review.yml (in the target repo)
name: Harness PR review
on: { pull_request: { types: [opened, synchronize] } }
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - name: Start pr_review
        env:
          CONDUCTOR_SERVER_URL: ${{ secrets.CONDUCTOR_SERVER_URL }}
        run: |
          curl -sf -X POST "$CONDUCTOR_SERVER_URL/workflow/pr_review" \
            -H 'Content-Type: application/json' \
            -d "{\"repo\":\"${{ github.repository }}\",\"prNumber\":${{ github.event.number }}}"
          # gate stays OFF for automation (no `approve`); AGENTS.md and .conductor/pr_review.md
          # (if committed) are applied by the worker automatically.
```

The worker reads a repo **agent guide** — `AGENTS.md` → `AGENT.md` → `CLAUDE.md` (first found at
the repo root) — and prepends it to the prompt of every coding/review/plan agent, across all
backends, so it learns how to build/test/review the repo with no payload. Disable per run with
`includeRepoGuide:false` or fleet-wide with `CODING_AGENT_REPO_GUIDE=0`. See
[`../docs/CODING_AGENT_WORKER.md`](../docs/CODING_AGENT_WORKER.md) §15.

## Backends

Every coding task selects its engine via `agent` (or `planAgent`/`codeAgent`/`designAgent`).
If unset, it's inferred from the model id: `gpt-*`/`o*`/`codex-*` → codex, `gemini-*` → gemini,
else claude. All three return the same result contract (status, result, structured output,
turns, tokens, cost) so they're interchangeable and mixable within one run. Cost is native for
Claude and estimated from token counts for Codex/Gemini. See
[`../docs/CODING_AGENT_WORKER.md`](../docs/CODING_AGENT_WORKER.md) §12 for the parity matrix.

## Guardrails

Coding agents run locked down: OS sandbox (writes confined to the worktree, no network unless
opened), a worktree-escape guard, a fixed tool allowlist (read/write/edit/search + scoped shell
incl. file move/delete, but not `rm -rf`/`sudo`/`git push`), and turn/budget/time circuit
breakers. Reviewers run **read-only**. Details in
[`../docs/CODING_AGENT_WORKER.md`](../docs/CODING_AGENT_WORKER.md) §5–§6.

## Running the workers

```bash
CONDUCTOR_SERVER_URL=http://localhost:8080/api workers/.venv/bin/python workers/main.py
```

`WORKER_MODULES` (comma-separated, default `coding_agent,gitops,campaign,openspec,automation,model_policy,revision`) selects which task modules
load; the default covers every workflow. `coding_agent` is the async agent driver
(`thread_count=8` = 8 concurrent sessions on one event loop); `gitops` holds the git/GitHub
tasks. Split them across hosts with `WORKER_MODULES` if desired — note the GitHub workflows
assume clone/code/push share a filesystem (single host, or a shared volume).

## Layout

```
main.py               entrypoint — loads WORKER_MODULES, starts the Conductor poller
common/               coding_agent (backend dispatch + locked-down Claude driver),
                      codex (openai-codex SDK + CLI fallback), gemini (Gemini CLI driver),
                      claude (SDK wrapper for merge conflict-resolution),
                      git (local + remote transport), github (gh/PR ops),
                      progress, session_store, cost, results, exec
coding_agent/         @worker_task("coding_agent") — the sandboxed coding worker + smoke_test.py
model_policy/         @worker_task("model_profile_resolve") — validates and resolves declarative profiles
revision/             bounded candidate checkpoint/evaluation workers for safe revision loops
gitops/               local: prepare_repo, create_branch, commit, worktree_add, merge_worktrees;
                      remote: git_clone/fetch/pull/push/remote, issue_fetch,
                      pr_comments/diff/create/checkout/status/comment/merge/submit_review
openspec/             OpenSpec source resolution, safe archive extraction, routing, verification,
                      and lifecycle workers; pinned local CLI package
workflows/            openspec_development, feature_campaign + campaign_subtask, code_parallel,
                      local_review, issue_to_pr, pr_review, address_pr, github_demo,
                      design_docs, code_subtask (+ taskdefs/)
```

Full design reference: **[`../docs/CODING_AGENT_WORKER.md`](../docs/CODING_AGENT_WORKER.md)**.
Agent operating guide: **[`../SKILL.md`](../SKILL.md)**.
