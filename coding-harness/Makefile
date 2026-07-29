# conductor coding-harness — operator shortcuts.
# Run `make` (or `make help`) to list targets.

SHELL  := /bin/bash
ROOT   := $(shell pwd)

# uv manages the Python toolchain: it provisions PY_VERSION (per .python-version /
# pyproject requires-python), downloading it if the host lacks it, so venvs never
# fall back to the system python3 (which has no claude-agent-sdk wheels).
UV         := uv
PY_VERSION := 3.13

# run.sh owns the worker/tui virtualenvs (workers/.venv, tui/.venv), .env
# loading, and server autostart; delegate to it so this Makefile never drifts.
RUN    := $(ROOT)/run.sh
WORKER_PY := $(ROOT)/workers/.venv/bin/python

# CI virtualenv: tui + worker deps + pytest, kept separate from the operator
# venvs so `make ci` is reproducible and never touches a running worker env.
VENV := $(ROOT)/.venv-ci
PY   := $(VENV)/bin/python
DEPS := $(VENV)/.deps-installed

export CONDUCTOR_SERVER_URL ?= http://localhost:8080/api

.DEFAULT_GOAL := help

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ── Conductor server ────────────────────────────────────────────────────────

server: ## Start the local Conductor server (OSS; Java 21+)
	@conductor server start

server-stop: ## Stop the local Conductor server
	@conductor server stop

server-logs: ## Tail the Conductor server log
	@conductor server logs

# ── Setup ───────────────────────────────────────────────────────────────────

venv: ## Create the worker virtualenv and install deps (workers/.venv)
	@$(RUN) setup

tui-venv: ## Create the TUI virtualenv and install deps (tui/.venv)
	@test -x $(ROOT)/tui/.venv/bin/python || $(UV) venv --python $(PY_VERSION) $(ROOT)/tui/.venv
	@$(UV) pip install -q --prerelease=explicit --python $(ROOT)/tui/.venv/bin/python -r $(ROOT)/tui/requirements.txt
	@echo "tui venv ready: $(ROOT)/tui/.venv"

# ── Definitions & workers ─────────────────────────────────────────────────────

register: ## Register/update task defs + workflows (starts a local server if needed)
	@$(RUN) register

WORKER_MODULES ?= coding_agent,gitops
workers: venv ## Run workers (supervised, foreground; WORKER_MODULES=coding_agent|gitops to split)
	@WORKER_MODULES=$(WORKER_MODULES) $(ROOT)/workers/run_workers.sh

run: ## Full bootstrap: setup + register + run workers (the one-command start)
	@$(RUN)

tui: ## Launch the terminal UI (chat; ANTHROPIC_API_KEY needed for chat)
	@$(RUN) tui

# ── Manual test: start a workflow run ─────────────────────────────────────────
# Each target builds its --input JSON with jq (safe quoting) and hands off to
# `conductor workflow start`. Pass extra conductor flags via ARGS='...'.

code: ## Code a change in a local repo: make code REPO=/abs/path INSTRUCTION="…" [DESIGN=true]
	@test -n "$(REPO)" && test -n "$(INSTRUCTION)" || { \
	  echo "usage: make code REPO=/abs/path/to/repo INSTRUCTION=\"do X\" [DESIGN=true]"; exit 1; }
	@input=$$(jq -cn --arg r "$(REPO)" --arg i "$(INSTRUCTION)" --argjson d $(if $(DESIGN),$(DESIGN),false) \
	  '{repoPath:$$r, instruction:$$i, design:$$d}'); \
	  conductor workflow start --workflow code_parallel --input "$$input" $(ARGS)

issue-to-pr: ## GitHub issue → PR: make issue-to-pr REPO=owner/name ISSUE=7
	@test -n "$(REPO)" && test -n "$(ISSUE)" || { \
	  echo "usage: make issue-to-pr REPO=owner/name ISSUE=<n>"; exit 1; }
	@input=$$(jq -cn --arg r "$(REPO)" --argjson n $(ISSUE) '{repo:$$r, issueNumber:$$n}'); \
	  conductor workflow start --workflow issue_to_pr --input "$$input" $(ARGS)

pr-review: ## Review a PR (posts a formal review): make pr-review REPO=owner/name PR=7
	@test -n "$(REPO)" && test -n "$(PR)" || { \
	  echo "usage: make pr-review REPO=owner/name PR=<n>"; exit 1; }
	@input=$$(jq -cn --arg r "$(REPO)" --argjson n $(PR) '{repo:$$r, prNumber:$$n}'); \
	  conductor workflow start --workflow pr_review --input "$$input" $(ARGS)

address-pr: ## Apply a PR's review feedback: make address-pr REPO=owner/name PR=7
	@test -n "$(REPO)" && test -n "$(PR)" || { \
	  echo "usage: make address-pr REPO=owner/name PR=<n>"; exit 1; }
	@input=$$(jq -cn --arg r "$(REPO)" --argjson n $(PR) '{repo:$$r, prNumber:$$n}'); \
	  conductor workflow start --workflow address_pr --input "$$input" $(ARGS)

github-demo: ## Clone → change → PR smoke test: make github-demo REPO_URL=… INSTRUCTION="…"
	@test -n "$(REPO_URL)" && test -n "$(INSTRUCTION)" || { \
	  echo "usage: make github-demo REPO_URL=<git-url> INSTRUCTION=\"do X\""; exit 1; }
	@input=$$(jq -cn --arg u "$(REPO_URL)" --arg i "$(INSTRUCTION)" '{repoUrl:$$u, instruction:$$i}'); \
	  conductor workflow start --workflow github_demo --input "$$input" $(ARGS)

watch: ## Follow a running execution: make watch ID=<workflowId>
	@test -n "$(ID)" || { echo "usage: make watch ID=<workflowId>"; exit 1; }
	@conductor workflow get-execution "$(ID)" -c

# ── Quality bar ───────────────────────────────────────────────────────────────

$(DEPS):
	@test -x $(PY) || $(UV) venv --python $(PY_VERSION) $(VENV)
	@$(UV) pip install -q --prerelease=explicit --python $(PY) -r tui/requirements.txt -r workers/requirements.txt pytest pytest-asyncio
	@touch $(DEPS)

validate: ## Validate workflow and task-definition JSON
	@jq empty workers/workflows/*.json workers/workflows/taskdefs/*.json

lint: $(DEPS) ## Compile Python and check shell syntax
	@$(PY) -m compileall -q tui workers
	@bash -n run.sh workers/register.sh workers/run_workers.sh

test: $(DEPS) ## Run unit tests (tui + workers)
	@$(PY) -m pytest tui/tests workers/tests -q

test-ci: test ## Alias for `test` (CI unit tests)

ci: validate lint test ## Run the coding-harness CI quality bar

# ── Cleanup ────────────────────────────────────────────────────────────────────

clean: ## Remove virtualenvs and harness runtime state (worktrees, sessions)
	@rm -rf $(VENV) workers/.venv tui/.venv \
	  $(ROOT)/.cc-worktrees $(ROOT)/.conductor-code && echo "cleaned"

.PHONY: help server server-stop server-logs venv tui-venv register workers run tui \
        code issue-to-pr pr-review address-pr github-demo watch \
        validate lint test test-ci ci clean
