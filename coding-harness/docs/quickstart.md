# Quickstart

## Prerequisites

- Python 3.13+, Java 21+, `jq`, and the `conductor` CLI.
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

Open <http://localhost:8080> for the execution graph and task details.

## Runtime timeouts

Runtime deadlines are configured only on Conductor task definitions. The workers do not wrap
Claude, Codex, Gemini, git, or merge-conflict sessions in an additional wall-clock timeout, and
the workflows do not expose a `timeoutS` input. For example, the shipped `coding_agent` task
definition currently uses `responseTimeoutSeconds: 86400`, `timeoutSeconds: 0`, and
`timeoutPolicy: ALERT_ONLY`. Change those task-definition values when deployment-specific timeout
behavior is required, then re-register with `./run.sh register`.

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
