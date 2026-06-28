# security-harness

> Part of **[conductor-agents](../README.md)** — a collection of production-grade agent harnesses orchestrated by Conductor.

An autonomous web-application & API **penetration-testing agent** orchestrated by **[Conductor](https://conductor-oss.org/)**.
Point it at a web app; it crawls with a real browser, reasons about the attack
surface with an LLM, runs a battery of scanners in parallel, **actively exploits** what it
finds (multi-identity, out-of-band confirmed), triages to cut false positives, and produces a
report — all as a durable, observable, retryable Conductor workflow. Optionally point it at the
**source code** too, and it mines the code (SAST + route extraction) to find more to test.

<!-- DEMO: drop a screen recording at docs/demo.gif and uncomment the line below.
<p align="center"><img src="docs/demo.gif" alt="security-harness demo" width="820"></p>
-->

## Run in ~30 seconds (local demo target)

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # server auto-enables the Anthropic provider from this
make venv && make up                  # build the worker venv + start the OWASP Juice Shop target (:3001)
./run.sh                              # auto-boots the stack and scans the bundled demo target
```

`./run.sh` with a URL/flags forwards straight to `./scan`/`./assess` (see below).

> ## ⚠️ Authorized testing only
> This tool sends traffic to and probes the targets you give it. Only scan
> systems you **own or have explicit written permission to test**. Authorization is
> **machine-enforceable**: pass a manifest (`--manifest`) — approvers, in-scope hosts,
> a testing window, a capability ceiling, rate/volume budgets, and forbidden operations
> — or `--authorized`, which synthesizes a minimal capability-bounded manifest. It is
> validated and **fails closed**. **Capability levels (0–4)** gate every action: reads
> are level 1, state-changing tests level 2, sensitive proof level 3, destructive level 4
> (prohibited by default); the harness can never raise its own level. A scope allowlist is
> enforced inside every worker, an independent safety governor halts the campaign on
> window expiry / a `--kill-switch` file / a policy breach, and a tamper-evident audit log
> records every action. Unauthorized scanning may be illegal. You are responsible for use.

## Why Conductor

Security scans are long-running, parallel, and failure-prone — exactly Conductor's
wheelhouse:

- **Durable** — a multi-hour scan survives worker/server restarts.
- **Parallel** — `FORK_JOIN_DYNAMIC` fans scanners out across every discovered endpoint.
- **Agentic** — `DO_WHILE` + `LLM_CHAT_COMPLETE` gives a ReAct browser agent.
- **Observable** — every task and finding is visible in the Conductor UI.
- **Schedulable** — recurring re-scans via cron schedules.

## Architecture

```
WORKFLOW security_scan
  normalize_target → authorization_gate (refuse if not authorized)
    → [source? → SAST sub-workflow → seed targets]      (Phase 3)
    → recon (passive) → web_crawl (Playwright + agent)   (Phase 1/2)
    → plan (LLM) → active_scan (FORK_JOIN_DYNAMIC)        (Phase 2)
    → triage (LLM: dedupe, severity, CWE/OWASP, cut FPs)
    → report_md (LLM) → report_pdf (GENERATE_PDF) → persist
```

LLM reasoning runs in native `LLM_CHAT_COMPLETE` tasks (Anthropic). Security tools
run in Dockerized workers. See [the plan](#roadmap) for what each phase adds.

## Prerequisites

- `conductor` CLI, Docker, Python 3.11+, `jq`, `curl`.
- `ANTHROPIC_API_KEY` exported **in the shell that starts the Conductor server**
  (the server auto-enables the provider from its environment). `./scan`/`./assess`
  auto-start the server, so export it before your first run.
- `make venv` once to build the worker virtualenv (deps + Playwright Chromium).

## Quickstart

One-time setup (build the worker venv; export your key):

```bash
cp .env.example .env                  # optional; defaults work for a local server
export ANTHROPIC_API_KEY=sk-ant-...   # the server auto-enables the Anthropic provider from this
make venv                             # build the worker virtualenv (installs deps + Playwright)
```

Then just point it at a target — **`./scan` auto-starts the stack for you**:

```bash
make up                               # start the OWASP Juice Shop test target on :3001 (test only)

./scan http://localhost:3001 --authorized
#   ℹ  conductor server: starting …        ← auto-started if not already up
#   ℹ  workflows: registering …            ← auto-registered if missing
#   ℹ  workers: starting fleet …           ← auto-started if not already polling
#   → http://localhost:8080/execution/<id>  (watch it live)
#   → reports/<scan-id>/report.pdf + report.md + findings.json + report.sarif
```

`./scan` and `./assess` **bootstrap the whole stack on demand** (server → register → worker
fleet), skipping any piece already healthy — so you don't have to start them by hand. To manage
the stack yourself (CI, a shared server), pass `--no-bootstrap` (or set `SC_NO_BOOTSTRAP=1`) and
bring it up manually with `make server && make register && make workers`.

Without `--authorized`, the workflow stops at the authorization gate by design:

```bash
./scan http://localhost:3001          # → TERMINATED at authorization_gate
./scan http://localhost:3001 --authorized
```

## Two entry points: `./scan` vs `./assess`

| | `./scan` (workflow `security_scan`) | `./assess` (workflow `deep_assess`) |
|---|---|---|
| Purpose | Fast surface scan: crawl → plan → DAST → triage → report | Deep app-aware **agentic pentest**: understand → hypothesize → **actively exploit** → adversarially verify, over iterative-deepening passes |
| Confirms bugs? | Reports candidates (+ active DAST hits) | **Yes** — re-runs each PoC and confirms blind bugs out-of-band; multi-identity cross-tenant testing |
| Typical time | ~3–5 min | tens of minutes |
| Capability | read/active DAST (`--intrusive`) | manifest + capability 0–4; product-feature exploitation needs `--capability 2` |

### `./scan` examples

```bash
# Passive + active surface scan (auto-bootstraps the stack):
./scan http://localhost:3001 --authorized

# Authenticated scan (test the API behind auth, not just map it):
./scan https://app.example.com --authorized \
  --auth-key "$KEY" --auth-secret "$SECRET" --token-url https://app.example.com/api/token \
  --scope app.example.com

# With source code (adds SAST + route extraction to seed the live scan), active checks on:
./scan https://app.example.com --authorized --source ./code --intrusive

# Use an already-running stack (skip auto-start):
./scan http://localhost:3001 --authorized --no-bootstrap
```

### `./assess` examples (deep agentic pentest)

```bash
# Two identities unlock BOLA / privilege-escalation / cross-tenant tests:
./assess https://app.example.com --authorized \
  --id 'userA=token:eyJ...A' --id 'userB=token:eyJ...B' --scope app.example.com

# Capability-2 product-feature exploitation against an Orkes/Conductor target, with docs + source,
# two tenants, purple-team + resilience, leaving tagged evidence for inspection:
./assess https://your-conductor.example.com --authorized --capability 2 \
  --profile conductor --docs https://orkes.io/content/ --source /path/to/source \
  --id 'orgA=key:K1,secret:S1,tokenurl:https://your-conductor.example.com/api/token,tenant:orgA' \
  --id 'orgB=key:K2,secret:S2,tokenurl:https://your-conductor.example.com/api/token,tenant:orgB' \
  --purple --resilience --require-tenants 2 --leave-evidence

# Pin the campaign at a specific catalog objective:
./assess https://app.example.com --authorized --capability 2 --objective INFRA-SSRF
```

> Capability-2 runs drive the target through the `code_exec` sandbox. Build its image once with
> `make codeexec-image`; for blind SSRF/RCE/exfil confirmation start an OOB collaborator with
> `make oob`. `./assess` preflights and refuses a cap-2 run if the sandbox image is missing.

### Authentication & SSO

The harness **carries** a credential you supply on every request/session — it never automates the
IdP login itself. Pick the path that matches how the target authenticates:

| Target auth | How to supply it |
|---|---|
| API key / service account | `--id 'u=key:<K>,secret:<S>,tokenurl:https://app/api/token'` — re-exchanged locally each run (best for long campaigns). `./scan` uses `--auth-key/--auth-secret/--token-url`. |
| Bearer/JWT you already have | `--id 'u=token:<JWT>'` (or `--auth-token` for `./scan`). `header:`/`scheme:` override the default `Authorization: Bearer`. |
| **SSO (Google/Okta/SAML)** | **`./sso-capture`** — log in once in a real browser, hand off the result. |

**SSO, one-step:** `./sso-capture` opens a real browser; you complete the SSO login; it sniffs the
auth header your app sends (scoped to the target's domain, so the IdP's own cookies are ignored),
captures the session, and writes a credential file:

```bash
./sso-capture https://app.example.com --label userA       # log in, press Enter to capture
#   ✓ captured bearer-sniffed credential for 'userA' → state/sessions/userA.json

./assess https://app.example.com --authorized \
  --id 'userA=session:state/sessions/userA.json'           # (repeat per persona/tenant for BOLA)
./scan  https://app.example.com --authorized --session state/sessions/userA.json
```

It captures the strongest available credential — a sniffed bearer token, else a localStorage JWT,
else the session cookie — and saves the full browser `storage_state` for the UI hand. For
interactive UI exploitation under SSO, also see `make chrome` + `SC_CDP_URL` (drive an
already-logged-in browser). Note: SSO access tokens expire — prefer an API-key credential for long
runs; re-run `./sso-capture` to refresh.

### Outputs (every run)

`reports/<id>/` contains `report.md`, `report.pdf`, `findings.json`, `report.sarif`
(SARIF 2.1.0 — ingestible by GitHub code scanning / DefectDojo / SIEM), and for `./assess` a
`dossier.json` (attack graph + confirmed/blind findings + residual-risk statement).

## Layout

```
conductor/   workflows, taskdefs, schedules, register.sh, ensure-stack.sh (auto-bootstrap)
workers/     common/ (scope, findings, sarif, voting, …), recon/ (base tasks), main.py, Dockerfile.*
prompts/     LLM system prompts (triage, report, exploit, verify, …) — injected at register time
catalog/     security-objective catalog + substrate pack (the data spine)
bench/       oracle/benchmark harness → reports/BENCH.md
scan         fast surface scan (security_scan)
assess       deep agentic pentest (deep_assess)
sso-capture  capture an SSO browser session → an --id credential
```

## Tooling & deployment modes

The scanners run either on the host worker or in Dockerized workers (the heavy/
risky tools default to containers):

| Mode | Command | Notes |
|------|---------|-------|
| Host workers | `make workers` | All task types; semgrep runs (pip); nuclei/sqlmap/dalfox/ffuf/gitleaks/trivy degrade gracefully if absent |
| Dockerized DAST | `make dast` | nuclei/sqlmap/dalfox/ffuf in a container; run host workers as `recon,browser` to avoid double-polling `active_check` |
| Dockerized SAST | `make sast SRC_ROOT=<dir>` | semgrep/gitleaks/trivy in a container; source mounted read-only at its real path (`SRC_ROOT`) |
| Persistent agent | `make chrome` + `SC_CDP_URL=http://127.0.0.1:9222` | `playwright_action` connects over CDP to one live browser, preserving in-page/SPA state across `--agent` steps (default is stateless cookie/URL replay) |
| Vector-RAG | `make rag` | Starts pgvector, reconfigures the server with a vectorDB instance (additive env), and indexes `knowledge/` (`rag_index`). `kb_retrieve` then pulls the most relevant remediation chunks into triage. Optional/gated — without it, triage falls back to the full static KB appended to its prompt. |
| Authenticated | `./scan <url> --auth-token <tok>` **or** `--auth-key <k> --auth-secret <s> --token-url <url>/api/token` | Resolves the credential once and threads it into recon, crawl (browser headers), API discovery, and every active check (+nuclei `-H`) — so the scan TESTS the API behind auth (IDOR/BOLA/SSRF/secrets-access), not just maps it. `--auth-header`/`--auth-scheme` for non-Bearer schemes. |

Notes: surface gathering (recon ∥ crawl ∥ API discovery ∥ SAST) runs concurrently
in a `FORK_JOIN` — SAST overlaps the crawl on `--source` scans. Remaining
wall-clock (~3-5 min) is dominated by the three sequential LLM calls
(plan → triage → report), which are on a true data dependency and can't overlap.

## Roadmap

- **Phase 0 ✅** — harness + thin slice: authorize → passive recon → LLM triage → PDF report.
- **Phase 1 ✅** — Playwright crawl + login (`web_crawl` sub-workflow); real URL/form/param/endpoint surface; LLM attack planner producing OWASP-mapped `planned_checks`.
- **Phase 2 ✅** — active DAST fanned out via `FORK_JOIN_DYNAMIC` executing the planner's `planned_checks` (Python probes for open-redirect/auth/IDOR/path-traversal/XSS/SQLi/CORS + nuclei/sqlmap/dalfox/ffuf in the Dockerized `dast-worker`); intrusive gating (`--intrusive`); confirmed findings merged into triage; `sanitize_md` keeps the PDF renderer happy.
- **Phase 2.5 ✅** — ReAct browser agent (`browser_agent` `DO_WHILE` sub-workflow): an LLM drives the browser (navigate/fill/click) via `playwright_action`, accumulating discovered surface. Enable with `--agent`.
- **Phase 3 ✅** — SAST on `--source` (semgrep `p/owasp-top-ten`+`p/security-audit`, run by the host worker via pip; gitleaks/trivy used when on PATH, else gracefully skipped) + framework-agnostic route extraction that seeds the live scan; API discovery (OpenAPI/Swagger/GraphQL) feeding endpoints into the surface. Enrichment steps are `optional` so a tool failure can't fail the scan.
- **Phase 4 ✅** — recurring scheduled scans (`make schedule URL=…`, Quartz cron); cross-scan `reports/DASHBOARD.md` rollup; polished PDF theming; a curated remediation knowledge base (`knowledge/`) injected into triage for grounded, referenced fixes. (Vector-RAG grounding via `LLM_INDEX_TEXT`/`LLM_SEARCH_INDEX` is available when a vectorDB integration is configured on the server; the static KB is the zero-dependency default.)
- **Strategic-grade hardening ✅** — toward [`SECURITY_HARNESS_SPEC.md`](SECURITY_HARNESS_SPEC.md): a machine-enforceable authorization manifest + capability levels + auto-halt/kill-switch; a **persistent cross-run knowledge store** (`state/`, keyed by deployment fingerprint — dedupes hypotheses across runs, invalidates findings on release); first-class **attacker personas** + a multi-dimensional **coverage ledger**; harness **self-defense** (prompt-injection guardrail, tamper-evident audit log, evidence/memory-integrity hashing); opt-in **purple-team** control assurance (`--purple`); a **benchmark harness** (`make bench` → FP/FN); and a **living dossier** (attack graph + residual-risk statement). See [CONFORMANCE.md](CONFORMANCE.md) for a section-by-section status table.

## Spec conformance

This repo implements much of [`SECURITY_HARNESS_SPEC.md`](SECURITY_HARNESS_SPEC.md). For a
section-by-section status (✅/🟡/❌) and the deliberate behavior choices (manifest-based
authorization, pass-boundary halts, honesty-over-assurance reporting), see
[**CONFORMANCE.md**](CONFORMANCE.md).
