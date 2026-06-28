# security-conductor — operator shortcuts.
# Run `make` (or `make help`) to list targets.

SHELL := /bin/bash
ROOT  := $(shell pwd)
VENV  := $(ROOT)/workers/.venv
PY    := $(VENV)/bin/python
PIP   := $(VENV)/bin/pip
export CONDUCTOR_SERVER_URL ?= http://localhost:8080/api
export REPORTS_DIR          ?= $(ROOT)/reports
export STATE_DIR            ?= $(ROOT)/state

.DEFAULT_GOAL := help

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

server: ## Start the local Conductor server (OSS, AI tasks enabled)
	@env -u CONDUCTOR_CONFIG_FILE conductor server start

server-stop: ## Stop the local Conductor server
	@conductor server stop

server-logs: ## Tail the Conductor server log
	@conductor server logs

up: ## Start the Juice Shop test target (Docker)
	@docker compose up -d juice-shop
	@echo "Juice Shop → http://localhost:3001"

down: ## Stop all Docker services
	@docker compose --profile workers down

venv: $(PY) ## Create the worker virtualenv and install deps
$(PY):
	@python3 -m venv $(VENV)
	@$(PIP) install -q --upgrade pip
	@$(PIP) install -q -r $(ROOT)/workers/requirements.txt
	@$(VENV)/bin/playwright install chromium
	@echo "venv ready: $(VENV)"

workers: venv ## Run workers on the host (recon + browser; foreground, ctrl-c to stop)
	@cd $(ROOT)/workers && REPORTS_DIR=$(REPORTS_DIR) WORKER_MODULES=$(WORKER_MODULES) $(PY) main.py

WORKER_MODULES ?= recon,browser,dast,sast,api,rag,httptool,codeexec,oob,safety,hc

codeexec-image: ## Build the code_exec sandbox image (deep-pentest agent's Docker hands)
	@docker compose --profile codeexec build code-exec-image
	@echo "sc-codeexec:latest built. The code_exec worker (host) spawns it per agent step."

sso: venv ## Capture an SSO session into an --id credential: make sso URL=https://app LABEL=userA
	@test -n "$(URL)" || { echo "usage: make sso URL=<app-url> [LABEL=userA] [SCOPE=h1,h2]"; exit 1; }
	@$(ROOT)/sso-capture "$(URL)" --label "$(or $(LABEL),user)" $(if $(SCOPE),--scope $(SCOPE),)

oob: ## Start the OOB collaborator (listener + cloudflared tunnel) for blind-bug confirmation
	@bash $(ROOT)/conductor/oob-setup.sh

oob-stop: ## Stop the OOB collaborator
	@bash $(ROOT)/conductor/oob-setup.sh --stop

workers-docker: ## Run the Dockerized base worker
	@docker compose --profile workers up -d --build base-worker
	@docker compose logs -f base-worker

dast: ## Run the Dockerized DAST worker (nuclei/sqlmap/dalfox/ffuf)
	@docker compose --profile dast up -d --build dast-worker
	@echo "dast-worker up. Run host workers as 'recon,browser' (WORKER_MODULES=recon,browser make workers)"
	@echo "so only the container serves active_check (with full tooling)."

SRC_ROOT ?= $(ROOT)
sast: ## Run the Dockerized SAST worker (semgrep/gitleaks/trivy). SRC_ROOT=<dir> mounts source.
	@SC_SOURCE_ROOT=$(SRC_ROOT) docker compose --profile sast up -d --build sast-worker
	@echo "sast-worker up (source root: $(SRC_ROOT)). Run host workers without 'sast' to avoid double-poll."

chrome: venv ## Launch a persistent CDP browser for --agent persistent sessions (foreground)
	@echo "Then start workers with SC_CDP_URL=http://127.0.0.1:9222 and scan with --agent."
	@cd $(ROOT)/workers && $(PY) chrome_server.py

rag: ## Enable vector-RAG: start pgvector, reconfigure server, index the KB
	@bash $(ROOT)/conductor/rag-setup.sh

register: ## Register task defs + workflows (injects prompts/)
	@bash $(ROOT)/conductor/register.sh

scan: ## Run a scan: make scan URL=http://localhost:3000
	@test -n "$(URL)" || { echo "usage: make scan URL=<target> [ARGS='--source ./x']"; exit 1; }
	@$(ROOT)/scan "$(URL)" --authorized $(ARGS)

schedule: ## Schedule recurring scans: make schedule URL=http://x [CRON='0 0 2 * * ?']
	@test -n "$(URL)" || { echo "usage: make schedule URL=<target> [CRON='0 0 2 * * ?'] [SRC=./code]"; exit 1; }
	@bash $(ROOT)/conductor/schedule.sh "$(URL)" "$(CRON)" "$(SRC)"

test: venv ## Run the unit test suite (scope, net, workflow jq)
	@$(VENV)/bin/pip install -q pytest && $(VENV)/bin/python -m pytest $(ROOT)/tests -q

# CI venv: worker deps + pytest + ruff, WITHOUT the Chromium download (the unit tests in tests/ are
# pure-logic and don't need a browser). Sentinel-backed so `lint` + `test-ci` share one build.
CIVENV_MARK := $(VENV)/.ci-deps
$(CIVENV_MARK):
	@python3 -m venv $(VENV)
	@$(PIP) install -q --upgrade pip
	@$(PIP) install -q -r $(ROOT)/workers/requirements.txt pytest ruff
	@touch $(CIVENV_MARK)

venv-ci: $(CIVENV_MARK) ## Build the CI venv (worker deps + pytest + ruff; no Chromium)

lint: venv-ci ## Lint workers/ + bench/ with ruff (config: ruff.toml)
	@$(VENV)/bin/ruff check $(ROOT)/workers $(ROOT)/bench

test-ci: venv-ci ## CI unit tests (no Chromium download; pure-logic tests only)
	@$(VENV)/bin/python -m pytest $(ROOT)/tests -q

bench: venv ## Oracle readiness (§19.2): catalog coverage + adversarial + held-out split -> reports/BENCH.md (offline, no server)
	@$(PY) $(ROOT)/bench/coverage.py $(ROOT)/bench/targets.json

bench-live: venv ## Quality metrics (spec 24): scan ground-truth apps, score live FP/FN + per-class -> reports/BENCH.md (needs server + workers)
	@echo "Requires a running server + workers + the targets in bench/targets.json."
	@$(PY) $(ROOT)/bench/run.py $(ROOT)/bench/targets.json

distill: venv ## Living oracle (§19.2): distill HUMAN-RATIFIED findings (ratified:true) from reports/ into bench/expected/ratified.json. RATIFY_ALL=1 ratifies all confirmed (operator asserts H6)
	@$(PY) $(ROOT)/bench/hc_writeback.py --distill $(if $(RATIFY_ALL),--ratify-all,)

retest: venv ## Re-test a prior dossier's PoCs (remediation verification): make retest DOSSIER=reports/<id>/dossier.json ID='key:..,secret:..,tokenurl:..'
	@test -n "$(DOSSIER)" || { echo "usage: make retest DOSSIER=reports/<id>/dossier.json ID='key:..,secret:..,tokenurl:..' [PROFILE=conductor]"; exit 1; }
	@$(PY) $(ROOT)/retest.py "$(DOSSIER)" --id "$(ID)" $(if $(PROFILE),--profile $(PROFILE),)

clean: ## Remove scan artifacts and the worker venv
	@rm -rf $(ROOT)/reports/* $(VENV) && echo "cleaned"

.PHONY: help server server-stop server-logs up down venv venv-ci lint test-ci workers workers-docker dast sast chrome rag register scan schedule test bench bench-live retest clean
