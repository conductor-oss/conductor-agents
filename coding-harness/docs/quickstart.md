# Quickstart

## Prerequisites

- Python 3.13+, Node.js 20.19+, npm, Java 21+, `jq`, and the `conductor` CLI.
- A reachable Conductor server, or permission for `./run.sh` to start one locally.
- At least one coding backend:
  - Claude: `claude login` or `ANTHROPIC_API_KEY`
  - Codex: `~/.codex/auth.json` or `OPENAI_API_KEY`
  - Gemini: Gemini CLI and `GEMINI_API_KEY`
- Authenticated `gh` CLI for GitHub workflows.

## Start the stack

From `coding-harness/`:

```bash
./run.sh
```

The script creates the worker environment, starts a local Conductor server when needed,
registers or updates task/workflow definitions, runs the SIMPLE-task worker gate, and starts the
workers. Set `CONDUCTOR_SERVER_URL` first to use a remote server.

## Run a local coding workflow

In another terminal:

```bash
export CONDUCTOR_SERVER_URL=http://localhost:8080/api
conductor workflow start --workflow code_parallel -i \
  '{"repoPath":"/absolute/path/to/repo","instruction":"Add a health endpoint and tests","design":false}'
```

The command returns a workflow ID. Monitor it with:

```bash
conductor workflow get-execution <workflowId> -c
```

The supplied directory remains on its current branch with all local edits intact. Agents work
in `<repoPath>/.cc-worktrees/run-<workflow-id>` from committed `HEAD`; uncommitted and untracked
source changes are reported but intentionally excluded. Add `"keepWorktree": false` to clean the
workspace after a successful run.

## Review local changes before committing

Use `local_review` when the changes already exist in your checkout and you want findings before
making a commit. It compares the working tree with a freshly fetched `origin/main` by default and
includes locally committed-ahead, staged, unstaged, and untracked files. It does not create a
worktree, alter files, stage, commit, push, or post a GitHub review.

```bash
conductor workflow start --workflow local_review -i '{
  "repoPath":"/absolute/path/to/repo",
  "baseRemote":"origin",
  "baseBranch":"main"
}'
```

The result contains the structured `summary`, `verdict`, and `comments`, plus the exact
`changedFiles` and `baseRef` reviewed.

Open <http://localhost:8080> for the execution graph and task details.

For a long-lived interactive feature, start a campaign instead:

```bash
conductor workflow start --workflow feature_campaign -i \
  '{"repoPath":"/absolute/path/to/repo","instruction":"Implement the new subsystem"}'
```

The branch defaults to `feature-campaign/<workflow-id>`. Open the run in the TUI to respond
to design, plan, wave, attached-server, and final-verification checkpoints. The campaign keeps
the branch local; it does not push or create a pull request.

To drive development from an apply-ready OpenSpec change in the same repository:

```bash
conductor workflow start --workflow openspec_development -i '{
  "repoPath": "/absolute/path/to/repo",
  "specSource": ".",
  "changeId": "add-health-endpoint"
}'
```

For a Git spec repository, use its clone URL as `specSource` and optionally set `specRef`.
To implement directly from a checked-out local spec source (including an ignored `design/openspec`
tree), use its absolute path and omit `repoPath`:

```bash
conductor workflow start --workflow openspec_development -i '{
  "specSource": "/absolute/path/to/repo/design/openspec",
  "changeId": "add-health-endpoint",
  "useSpecSourceWorkspace": true
}'
```

This creates an isolated worktree in that repository and opens a draft PR after verification.
For a public HTTPS archive, set `specWritebackRepo` to the GitHub repository where the completed
change must be archived. Auto mode chooses `code_parallel` or `feature_campaign`; set
`executionMode` only when you need an explicit override.

## Runtime timeouts

Runtime deadlines are configured only on Conductor task definitions. The workers do not wrap
Claude, Codex, Gemini, git, or merge-conflict sessions in an additional wall-clock timeout, and
the workflows do not expose a `timeoutS` input. For example, the shipped `coding_agent` task
definition currently uses `responseTimeoutSeconds: 86400`, `pollTimeoutSeconds: 300`,
`timeoutSeconds: 86400`, and `timeoutPolicy: TIME_OUT_WF`. Change those task-definition values
when deployment-specific timeout behavior is required, then re-register with
`./run.sh register`.

## Optional TUI

```bash
cd tui
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd ..
CONDUCTOR_SERVER_URL=http://localhost:8080/api tui/.venv/bin/python -m tui
```

The TUI supports chat, forms, a live dashboard, editable human gates, and confirmed workflow
registration. `ANTHROPIC_API_KEY` is required for TUI chat but not for forms/dashboard.
