---
name: coding-harness
description: >-
  Drive autonomous coding as durable Conductor workflows. Use when the user wants to
  resolve a GitHub issue into a PR, review a pull request or a local checkout before commit,
  address PR feedback, or make a parallelized multi-part code change in a repo. Also use to register/update the harness
  definitions or operate its TUI. Triggers Conductor workflows
  (issue_to_pr, pr_review, local_review, address_pr, code_parallel) via the `conductor` CLI; coding runs
  on Claude / Codex / Gemini backends in sandboxed git worktrees.
---

# Conductor Coding Harness

A set of Conductor workflows that run coding agents autonomously and durably. You (the
agent) don't write the code yourself — you **trigger the right workflow** and report the
result (PR URL, review, cost). The harness handles planning, parallel coding in isolated
worktrees, git/GitHub plumbing, guardrails, retries, and observability.

## Choose a workflow

| The user wants to… | Trigger workflow |
|---|---|
| Turn a GitHub issue into a pull request | `issue_to_pr` |
| Review an open PR and post comments | `pr_review` |
| Review local changes before committing, without modifying or posting anything | `local_review` |
| Apply the review feedback on a PR | `address_pr` |
| Review each new PR revision on a schedule | `pr_review_sweep` |
| Address changed feedback on harness-created PRs | `pr_address_sweep` |
| Resolve labeled issues on a schedule | `issue_resolution_sweep` |
| Make a multi-part code change in a local repo (no GitHub) | `code_parallel` |
| Smoke-test GitHub connectivity (clone→change→PR) | `github_demo` |

The natural loop: **`issue_to_pr` → `pr_review` → `address_pr`** (open, review, revise).

## Before triggering (preflight)

1. **Workers must be running and definitions registered.** Check the server is reachable
   (`conductor workflow list`) and the workflow exists (`conductor workflow get <name>`).
   If definitions are missing or changed, run `./workers/register.sh`; from the TUI use
   `/register` in chat or `g` on the dashboard. Registration updates existing definitions,
   creates missing ones, and verifies every referenced SIMPLE task has a registered task
   definition. If workers are down, SIMPLE tasks will still queue and wait: start them with
   `workers/.venv/bin/python workers/main.py`.
   Registration requires both the `conductor` CLI and `jq`.
2. **Auth must be in the worker's environment**, not yours: `gh auth login` (or `GH_TOKEN`)
   for the GitHub workflows, and the chosen backend's key (`ANTHROPIC_API_KEY` /
   `~/.codex/auth.json` / `GEMINI_API_KEY`). You can't fix these from here — surface them.
3. **Pick a backend** only if the user cares; otherwise omit and it defaults to `claude`.
   Mix with `openspecPlanAgent`/`codeAgent` when asked (e.g. plan on claude, code on codex).
4. **Choose the publication gate.** TUI launches default `pr_review.approve=true` and
   `issue_to_pr.approvePr=true`, pausing before anything is posted/opened. Raw CLI/API runs
   default both gates off unless explicitly supplied.

## 60-second setup

From the `coding-harness/` directory, assuming the prerequisites above are installed:

```bash
./run.sh
```

This starts a local server when needed, installs worker dependencies, registers definitions,
runs the worker gate, and starts the worker fleet. Set `CONDUCTOR_SERVER_URL` first when using
a remote server.

Then, in another terminal:

```bash
export CONDUCTOR_SERVER_URL=http://localhost:8080/api
conductor workflow start --workflow code_parallel --input \
  '{"repoPath":"/absolute/path/to/repo","instruction":"Add a health endpoint and tests"}'
```

Use `./run.sh setup`, `./run.sh register`, or `./run.sh tui` for individual operations.

## How to trigger

```bash
conductor workflow start --workflow <name> -i '<json>'
```

Then poll to completion and report the output:

```bash
conductor workflow get-execution <workflowId> -c    # status + tasks + output
conductor workflow status <workflowId>              # quick status
```

Long runs are normal (minutes). Don't block tightly — check periodically. Every run reports
`totalTokens`/`totalCostUsd` (or `tokenUsed`/`costUsd`); relay the PR/review URL and cost.

## Workflow inputs (essentials)

Only the **required** inputs must be set; everything else has sane defaults. The complete,
definition-backed required/optional tables are in
[`docs/workflow-inputs.md`](docs/workflow-inputs.md).

- **`issue_to_pr`** — required: `repo` (URL or `owner/name`), `issueNumber`. Common:
  `base` (`main`), `codeAgent`, `openspecHumanApproval` (`true`).
  → outputs `prNumber`, `prUrl`.
- **`pr_review`** — required: `repo`, `prNumber`. Common: `agent`, `model`, `approve`.
  → posts a formal review (inline comments + summary; COMMENT or REQUEST_CHANGES, never
  APPROVE); outputs `reviewUrl`, `event`, `inlineCount`.
- **`local_review`** — required: `repoPath`. Common: `baseRemote` (`origin`),
  `baseBranch` (`main`), `agent`. Fetches that remote branch and reviews the existing checkout
  directly, including local commits, staged/unstaged edits, and untracked files. It never
  creates a worktree, writes source files, stages, commits, pushes, or posts a GitHub review.
  → outputs `summary`, `verdict`, `comments`, `changedFiles`, `baseRef`.
- **`address_pr`** — required: `repo`, `prNumber`. Common: `engine` (`code_parallel` default,
  or `coding_agent` for small feedback), `agent`, `model`, `design`. Pushes to the PR's own
  branch; re-runnable.
  → outputs `pushed`, `replyUrl`.
- **`code_parallel`** — required: `repoPath` (local dir), `instruction`. Common:
  `changeBranch`, `openspecHumanApproval`, `openspecPlanAgent`/`codeAgent`. Local only — no
  clone/push. → outputs `changeBranch`, `merged`, `totalTokens`, `totalCostUsd`.
- **`github_demo`** — required: `repoUrl`, `instruction`. Connectivity smoke test only;
  clone → one coding session → push → PR.

Internal workflows are `openspec_plan`, `openspec_generate_artifact`, and `code_subtask`;
normally let `code_parallel` invoke them rather than starting them directly.

`code_parallel`, `issue_to_pr`, and `address_pr` (with its default `code_parallel` engine)
always plan through OpenSpec first — `openspec_plan` drives the `openspec` CLI to produce a
proposal/specs/design/tasks change, then deterministically parses the generated `tasks.md`
into the independent sub-tasks that fan out in parallel. There's no "skip planning" toggle.
Human review of the generated plan is the default (`openspecHumanApproval:true`): approval
exits the bounded plan loop, while feedback triggers a revision. Set
`openspecHumanApproval:false` only when the user wants the read-only coding-agent judge
instead. The default is five plan iterations; use `openspecMaxIterations` when the user
requests a higher limit.

Shared tuning knobs (all optional): `maxTurns`, `maxBudgetUsd`, `*Model` (`""` =
backend default). `modelProfile` is available on every workflow; pass a profile name or leave it
blank to use the configured default. Backends: `claude` (default) | `codex` | `gemini`, or
inferred from a `*Model` id. Shipped workflows default every applicable agent budget to `$50` and every turn cap
to at least 250; `code_parallel`'s OpenSpec planning and coding sessions default to 500 turns.

**Prompt templates (optional).** To fully override an agent step's prompt with your own
instructions (review focus, house style, domain rules), either pass a `*PromptTemplate` input
(`localReviewPromptTemplate`, `reviewPromptTemplate`, `codePromptTemplate`, `planPromptTemplate`, `designPromptTemplate`,
`fixPromptTemplate`, plus campaign/OpenSpec and approval/design-judge variants) or commit a `.conductor/<key>.md` file in the target repo
(`local_review`/`pr_review`/`code`/`plan`/`design`/`address_pr`) — the repo file applies automatically with no
input, which is ideal for scheduled/CI runs. A `*PromptTemplate` input may also be `@repo/path`
to read the prompt from a repo file. The canonical default prompts live in
`workers/defaults/prompts/`. OpenSpec artifact generation (proposal/specs/design/tasks) is instead
driven by that artifact's `openspec instructions` output, not a `*PromptTemplate` input.
Every such input has a paired `*PromptTemplateSource`; pass a descriptive origin when launching
through an API. The worker reports the actual resolved source, key, and SHA-256 hash in
`output.promptTemplate`, which is authoritative.
The TUI's form, chat, and schedule launch paths consult `~/.conductor-harness/templates` for every
prompt role before starting. Role-specific files declare `fields: [planPromptTemplate]` (or another
input name); legacy files without `fields` apply to the workflow's primary role. A unique match is
copied into the durable input with `user:<path>` provenance, while ambiguous same-role matches
block the launch. Blank roles continue through repo `.conductor/<key>.md` and bundled fallback.

**Repo guide (`AGENTS.md`).** The worker auto-reads a repo guide — `AGENTS.md` → `AGENT.md` →
`CLAUDE.md` (first at the repo root) — and injects it into every agent's prompt (coding, review,
plan) across all backends, so it learns how to build/test/review with no payload. Put build/test
commands + review priorities there. Toggle: `includeRepoGuide` / `CODING_AGENT_REPO_GUIDE=0`. Explicit input wins over the repo file. `{{diff}}`/`{{feedback}}`
/`{{instruction}}`/`{{subtask}}` placeholders in the template are filled with runtime context;
the output schema stays enforced (a custom `pr_review` template still yields a structured review).
See `docs/CODING_AGENT_WORKER.md` §14.

For the complete operator contract, use `docs/model-profiles.md` for policy/profile selection,
`docs/templates.md` for prompt precedence and trust boundaries, and `docs/openspec.md` for
checked-out local OpenSpec source/worktree behavior. The workflow JSON input contract is kept in
`docs/workflow-inputs.md`.

## Examples

```bash
# Resolve issue #42 into a PR
conductor workflow start --workflow issue_to_pr \
  -i '{"repo":"https://github.com/acme/app.git","issueNumber":42}'

# Review PR #7
conductor workflow start --workflow pr_review \
  -i '{"repo":"acme/app","prNumber":7}'

# Review local changes before committing (read-only, no GitHub side effects)
conductor workflow start --workflow local_review \
  -i '{"repoPath":"/absolute/path/to/repo","baseRemote":"origin","baseBranch":"main"}'

# Address the feedback on PR #7 (cheap single-session engine)
conductor workflow start --workflow address_pr \
  -i '{"repo":"acme/app","prNumber":7,"engine":"coding_agent"}'
```

## Guardrails you can rely on

- Coding agents are sandboxed to the worktree (no escape, no network unless opened), with a
  fixed tool allowlist and turn/budget/time caps. Reviewers are **read-only**.
- `pr_review` never approves; destructive ops (`pr_merge`) are separate and opt-in.
- `local_review` is read-only for source files and GitHub: it refreshes only the selected
  remote-tracking baseline, then returns local review findings.
- The harness pushes to a change branch and opens/updates PRs — it does not merge or
  force-push unless a workflow explicitly does so. Confirm with the user before merging.
- Interactive TUI mutations require confirmation. Review/PR publication gates can be edited,
  approved, rejected, or deferred before remote side effects happen.

## TUI operations

Install and launch from `coding-harness/`:

```bash
python3 -m venv tui/.venv
tui/.venv/bin/pip install -q -r tui/requirements.txt
CONDUCTOR_SERVER_URL=http://localhost:8080/api tui/.venv/bin/python -m tui
```

Important commands: `/dashboard`, `/open [workflowId]`, `/folder [workflowId]`,
`/templates`, `/register`, `/sessions`, and `/help`. The dashboard uses `g` to register
definitions. `ANTHROPIC_API_KEY` is needed for conversational chat, but forms/dashboard work
without it.

Chat may start at most one workflow per user message. If a request could map to multiple
workflows, it asks the user to choose one before starting anything. Natural-language requests
to register, re-register, update, or refresh definitions invoke the same confirmed registration
flow as `/register`, including the SIMPLE-task worker gate.

## Automation schedules

The sweep and dispatch workflows accept `approvalMode: human|llm|none` and default to `human`.
Use the [workflow input reference](docs/workflow-inputs.md) for their complete contracts.

Automation schedules use Quartz cron (default `0 */10 * ? * *`), local timezone, and no
catch-up. Never put credentials in schedule input. Schedule mutation/run-now, item reset, and
approval decisions require confirmation. The TUI dashboard keys are `s` for Automations and
`a` for the global Approval Inbox.

## Boundaries / gotchas

- **Same-host filesystem**: the GitHub workflows clone into a temp folder and code/push there;
  they assume one worker host (or a shared volume). Fine for a single-worker deployment.
- **Same-repo PRs**: `issue_to_pr` / `address_pr` target repos you can push to; fork-based
  contribution isn't wired yet.
- **Large PRs**: `pr_review` caps the diff (~200 KB) — very large PRs get a partial review.
- If a workflow **hangs in RUNNING** with no task progress, a SIMPLE task's worker isn't
  polling (workers down, or `WORKER_MODULES` missing `coding_agent`/`gitops`/`openspecops`) —
  check the worker process, not the workflow.
- **`openspec` CLI required**: `openspec_plan` shells out to it (via the `openspecops` worker
  module), so it must be installed on every host running workers.
- If a run's logs show `NonTransientException: [SQLITE_BUSY]` / `[SQLITE_BUSY_SNAPSHOT]`, the
  default local server's SQLite backend is hitting write-lock contention (common with
  `code_parallel`/`openspec_plan`'s parallel fan-out). Suggest the opt-in Postgres-backed server:
  `CONDUCTOR_BACKEND=postgres` in `.env`, then re-run `./run.sh` (needs Docker) — see
  [`docker-compose.postgres.yml`](docker-compose.postgres.yml).

Full reference: [`docs/CODING_AGENT_WORKER.md`](docs/CODING_AGENT_WORKER.md). User guide with
complete input tables: [`workers/README.md`](workers/README.md).
