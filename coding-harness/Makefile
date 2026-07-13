SHELL := /bin/bash
PYTHON ?= python3
VENV := .venv-ci
PY := $(VENV)/bin/python
DEPS := $(VENV)/.deps-installed

.DEFAULT_GOAL := help

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(DEPS):
	@test -x $(PY) || $(PYTHON) -m venv $(VENV)
	@$(VENV)/bin/pip install -q -r tui/requirements.txt pytest pytest-asyncio
	@touch $(DEPS)

validate: ## Validate workflow and task-definition JSON
	@jq empty workers/workflows/*.json workers/workflows/taskdefs/*.json

lint: ## Compile Python and check shell syntax
	@$(PYTHON) -m compileall -q tui workers
	@bash -n run.sh workers/register.sh workers/run_workers.sh

test-ci: $(DEPS) ## Run unit tests
	@$(PY) -m pytest tui/tests workers/tests -q

ci: validate lint test-ci ## Run the coding-harness CI quality bar

.PHONY: help validate lint test-ci ci
