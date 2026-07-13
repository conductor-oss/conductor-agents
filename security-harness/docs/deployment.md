# Deployment modes

The worker fleet can run entirely on the host, or with heavier tooling isolated in containers. Mix and match to fit your environment.

## Deployment modes

| Mode | Command | Notes |
|---|---|---|
| Host workers | `make workers` | All task types; semgrep via pip; nuclei/sqlmap/dalfox/ffuf/gitleaks/trivy degrade gracefully if absent |
| Dockerized DAST | `make dast` | nuclei/sqlmap/dalfox/ffuf in a container; run host workers as `recon,browser` to avoid double-polling `active_check` |
| Dockerized SAST | `make sast SRC_ROOT=<dir>` | semgrep/gitleaks/trivy in a container; source mounted read-only at its real path |
| Persistent browser | `make chrome` + `SC_CDP_URL=http://127.0.0.1:9222` | Playwright connects over CDP to one live browser, preserving in-page/SPA state across `--agent` steps |
| Vector-RAG | `make rag` | Starts pgvector, indexes `knowledge/`; triage pulls relevant remediation chunks rather than the full static KB |

## Project layout

| Path | Contents |
|---|---|
| `conductor/` | Workflow JSON, task definitions, schedules, `register.sh`, `ensure-stack.sh` (auto-bootstrap) |
| `workers/` | Task workers — `common/` (scope, findings, SARIF, voting), `recon/`, `main.py`, Dockerfiles |
| `prompts/` | LLM system prompts (triage, report, exploit, verify) — injected at register time |
| `catalog/` | Security-objective catalog + substrate pack (the data spine) |
| `bench/` | Benchmark/oracle harness → `reports/BENCH.md` |
| `sast` | Entry point: source-only static assessment (`sast_report` workflow) — no live site |
| `scan` | Entry point: fast surface scan (`security_scan` workflow) |
| `assess` | Entry point: deep agentic pentest (`deep_assess` workflow) |
| `sso-capture` | Capture an SSO browser session → an `--id` credential |
