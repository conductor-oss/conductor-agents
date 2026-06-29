# security-harness

> Part of **[conductor-agents](../README.md)** — a catalog of production-grade AI agents orchestrated by Conductor.

An autonomous web-application & API **penetration-testing agent** orchestrated by **[Conductor](https://conductor-oss.org/)**.
Point it at a web app; it crawls with a real browser, reasons about the attack surface with an LLM,
runs a battery of scanners in parallel, **actively exploits** what it finds (multi-identity, out-of-band confirmed),
triages to cut false positives, and produces a report — all as a durable, observable, retryable Conductor workflow.
Optionally point it at the **source code** too, and it mines the code (SAST + route extraction) to find more to test.

<!-- DEMO: drop a screen recording at docs/demo.gif and uncomment the line below.
<p align="center"><img src="docs/demo.gif" alt="security-harness demo" width="820"></p>
-->

> **⚠️ Authorized testing only.** Only scan systems you **own or have explicit written permission to test.**
> Unauthorized scanning may be illegal. You are responsible for use.
> See [Authorization & capability levels](#authorization--capability-levels) for machine-enforceable controls.

---

## Prerequisites

- `conductor` CLI, Docker, Python 3.11+, `jq`, `curl`
- `ANTHROPIC_API_KEY` — export it before your first run; `./scan`/`./assess` auto-start the Conductor server and it picks the key up from the environment
- Run `make venv` once to build the worker virtualenv (installs Python deps + Playwright Chromium)

---

## Quickstart

One-time setup:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make venv                             # build worker venv — do this once
make up                               # start OWASP Juice Shop (an intentionally vulnerable demo app, safe to scan) on :3001
```

Then scan it — **`./scan` auto-starts the full Conductor stack for you**:

```bash
./scan http://localhost:3001 --authorized
#   ℹ  conductor server: starting …        ← auto-started if not already up
#   ℹ  workflows: registering …            ← auto-registered if missing
#   ℹ  workers: starting fleet …           ← auto-started if not already polling
#   → http://localhost:8080/execution/<id>  (watch it live in the Conductor UI)
#   → reports/<scan-id>/report.pdf + report.md + findings.json + report.sarif
```

When you're done: `make down` tears everything down.

> **CI / shared server:** pass `--no-bootstrap` (or `SC_NO_BOOTSTRAP=1`) to skip auto-start and manage the stack yourself:
> `make server && make register && make workers`

Without `--authorized`, the workflow stops at the authorization gate by design — this is intentional:

```bash
./scan http://localhost:3001          # → TERMINATED at authorization_gate
./scan http://localhost:3001 --authorized
```

---

## `./scan` vs `./assess`

Two entry points for different testing depths. (`./run.sh <url> [flags]` is a convenience alias that forwards to one of these.)

| | `./scan` — `security_scan` workflow | `./assess` — `deep_assess` workflow |
|---|---|---|
| **Purpose** | Fast surface scan: crawl → plan → DAST → triage → report | Deep agentic pentest: understand → hypothesize → **actively exploit** → adversarially verify, over iterative-deepening passes |
| **Confirms bugs?** | Reports candidates (+ active DAST hits) | **Yes** — re-runs each PoC and confirms blind bugs out-of-band; multi-identity cross-tenant testing |
| **Typical time** | ~3–5 min | Tens of minutes |
| **Authorization** | `--authorized` for quick tests; `--manifest` for real engagements | Same; `--manifest` recommended for anything beyond local demo |
| **Capability** | read / active DAST (`--intrusive`) | 0–4; product-feature exploitation needs `--capability 2` |

### `./scan` examples

```bash
# Passive + active surface scan:
./scan http://localhost:3001 --authorized

# Authenticated scan — tests the API behind auth (IDOR/BOLA/SSRF), not just maps it:
./scan https://app.example.com --authorized \
  --auth-key "$KEY" --auth-secret "$SECRET" --token-url https://app.example.com/api/token \
  --scope app.example.com

# With source code — adds SAST + route extraction to seed the live scan:
./scan https://app.example.com --authorized --source ./code --intrusive

# Already-running stack (skip auto-start):
./scan http://localhost:3001 --authorized --no-bootstrap
```

### `./assess` examples

```bash
# Two identities unlock BOLA / privilege-escalation / cross-tenant tests:
./assess https://app.example.com --authorized \
  --id 'userA=token:eyJ...A' --id 'userB=token:eyJ...B' --scope app.example.com

# Capability-2 deep pentest — docs + source + two tenants + purple team:
./assess https://your-conductor.example.com --authorized --capability 2 \
  --profile conductor --docs https://orkes.io/content/ --source /path/to/source \
  --id 'orgA=key:K1,secret:S1,tokenurl:https://your-conductor.example.com/api/token,tenant:orgA' \
  --id 'orgB=key:K2,secret:S2,tokenurl:https://your-conductor.example.com/api/token,tenant:orgB' \
  --purple --resilience --require-tenants 2 --leave-evidence

# Pin to a specific objective:
./assess https://app.example.com --authorized --capability 2 --objective INFRA-SSRF
```

> **Capability-2 prerequisites:** build the sandbox image once with `make codeexec-image`. For blind SSRF/RCE/exfil confirmation, start an OOB collaborator with `make oob`. `./assess` preflights and refuses a cap-2 run if the sandbox image is missing.

---

## Authentication & SSO

The harness carries a credential you supply on every request — it never automates the IdP login itself.

| Target auth | How to supply it |
|---|---|
| API key / service account | `--id 'u=key:<K>,secret:<S>,tokenurl:https://app/api/token'` — re-exchanged locally each run (best for long campaigns). `./scan` uses `--auth-key/--auth-secret/--token-url`. |
| Bearer/JWT you already have | `--id 'u=token:<JWT>'` (or `--auth-token` for `./scan`). Use `header:`/`scheme:` to override `Authorization: Bearer`. |
| SSO (Google/Okta/SAML) | `./sso-capture` — log in once in a real browser, hand off the result (see below). |

**SSO in one step:** `./sso-capture` opens a real browser; you complete the login; it sniffs the auth header your app sends (scoped to the target domain — IdP cookies are ignored), and writes a credential file:

```bash
./sso-capture https://app.example.com --label userA
#   ✓ captured bearer-sniffed credential for 'userA' → state/sessions/userA.json

./assess https://app.example.com --authorized --id 'userA=session:state/sessions/userA.json'
./scan   https://app.example.com --authorized --session state/sessions/userA.json
```

It captures the strongest available credential — sniffed bearer token → localStorage JWT → session cookie — and saves the full browser `storage_state`. Note: SSO access tokens expire; prefer API-key credentials for long runs and re-run `./sso-capture` to refresh.

For interactive UI exploitation with an already-logged-in browser, see `make chrome` + `SC_CDP_URL`.

---

## Outputs

Every run writes to `reports/<scan-id>/`:

| File | Contents |
|---|---|
| `report.pdf` / `report.md` | Human-readable findings report |
| `findings.json` | Structured findings — severity, CWE, OWASP, reproduction steps |
| `report.sarif` | SARIF 2.1.0 — ingest into GitHub code scanning, DefectDojo, or your SIEM |
| `dossier.json` | (`./assess` only) Attack graph + confirmed/blind findings + residual-risk statement |

---

## Architecture

```
WORKFLOW security_scan
  normalize_target → authorization_gate (refuse if not authorized)
    → [source? → SAST sub-workflow → seed targets]
    → recon (passive) → web_crawl (Playwright + agent)
    → plan (LLM) → active_scan (FORK_JOIN_DYNAMIC)
    → triage (LLM: dedupe, severity, CWE/OWASP, cut FPs)
    → report_md (LLM) → report_pdf (GENERATE_PDF) → persist
```

LLM reasoning runs in native `LLM_CHAT_COMPLETE` tasks (Anthropic). Security tools run in Dockerized workers. Surface gathering (recon, crawl, API discovery, SAST) runs concurrently in a `FORK_JOIN` — the three sequential LLM steps (plan → triage → report) are on a true data dependency and cannot overlap.

---

## Deployment modes

| Mode | Command | Notes |
|---|---|---|
| Host workers | `make workers` | All task types; semgrep via pip; nuclei/sqlmap/dalfox/ffuf/gitleaks/trivy degrade gracefully if absent |
| Dockerized DAST | `make dast` | nuclei/sqlmap/dalfox/ffuf in a container; run host workers as `recon,browser` to avoid double-polling `active_check` |
| Dockerized SAST | `make sast SRC_ROOT=<dir>` | semgrep/gitleaks/trivy in a container; source mounted read-only at its real path |
| Persistent browser | `make chrome` + `SC_CDP_URL=http://127.0.0.1:9222` | Playwright connects over CDP to one live browser, preserving in-page/SPA state across `--agent` steps |
| Vector-RAG | `make rag` | Starts pgvector, indexes `knowledge/`; triage pulls relevant remediation chunks rather than the full static KB |

---

## Project layout

| Path | Contents |
|---|---|
| `conductor/` | Workflow JSON, task definitions, schedules, `register.sh`, `ensure-stack.sh` (auto-bootstrap) |
| `workers/` | Task workers — `common/` (scope, findings, SARIF, voting), `recon/`, `main.py`, Dockerfiles |
| `prompts/` | LLM system prompts (triage, report, exploit, verify) — injected at register time |
| `catalog/` | Security-objective catalog + substrate pack (the data spine) |
| `bench/` | Benchmark/oracle harness → `reports/BENCH.md` |
| `scan` | Entry point: fast surface scan (`security_scan` workflow) |
| `assess` | Entry point: deep agentic pentest (`deep_assess` workflow) |
| `sso-capture` | Capture an SSO browser session → an `--id` credential |

---

## Authorization & capability levels

Use `--authorized` for quick tests and CI — it synthesizes a minimal, capability-bounded manifest automatically.

For real engagements, use `--manifest <file>` instead. A manifest specifies: approvers, in-scope hosts, testing window, capability ceiling, rate/data-volume budgets, allowed techniques, and forbidden operations / protected records. It is validated at startup and **fails closed**.

**Capability levels gate every action.** The ceiling comes from the manifest (`--capability <0-4>`, default **1**); every worker refuses an action whose required level exceeds it, and the harness can never raise its own level.

| Level | Permits | HTTP verbs / actions | Mutates the target? |
|---|---|---|---|
| **0** | Passive reading / observation | recon only, no active requests | No |
| **1** *(default)* | Reversible, low-volume active probes | `GET` / `HEAD` / `OPTIONS` | **No — writes & `code_exec` refused at the gate** |
| **2** | State-changing tests with **synthetic data**; product-feature exploitation | `POST` / `PUT` / `PATCH` / `DELETE`, `code_exec` | Creates only its own `sc-pentest-<run>-` objects, ledgered and auto-cleaned |
| **3** | Potentially sensitive / operationally risky proof | as L2, just-in-time approved | As L2 (sensitive scope) |
| **4** | Destructive / availability-impacting / real-data extraction | — | **Prohibited by default; cannot self-escalate** |

The bounded availability / denial-of-wallet tier (load probing) is **off** unless you pass `--resilience`.

### Running without mutating the target

- **Guaranteed read-only:** use `--capability 1` (the default). Writes and `code_exec` are refused by the capability gate in every worker — zero mutations, zero destruction — while you still get the full read-based active surface (GET-param SQLi/XSS/traversal/open-redirect/CORS, recon, crawl, SAST). Trade-off: state-changing classes (e.g. SSRF that requires *creating and running* a workflow) aren't reachable.
- **Capability 2 without touching existing resources:** level 2 operates only on synthetic, prefixed objects it creates and then cleans up; destructive/availability actions are level 4 (off). To turn that convention into a *hard* fence, add `forbidden_operations` / `protected_records` to a `--manifest` — they are enforced on direct HTTP calls **and inside the `code_exec` sandbox**. (Avoid a blanket `DELETE *`, which would also block cleanup of the synthetic objects.)

An independent safety governor halts the campaign on window expiry, a `--kill-switch` file, a rate/data-volume budget breach, or a policy breach. A tamper-evident audit log records every action.

---

## Why Conductor

Security scans are long-running, parallel, and failure-prone — exactly the problem Conductor was built to solve. See the [parent README](../README.md#why-conductor) for the full breakdown.
