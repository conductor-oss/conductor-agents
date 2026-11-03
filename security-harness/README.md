# Conductor Security Harness

> Part of **[conductor-agents](../README.md)** — production-grade AI agents orchestrated by Conductor.

An autonomous web-application & API **penetration-testing agent** orchestrated by **[Conductor](https://conductor-oss.org/)**. Point it at a target and it crawls with a real browser, reasons about the attack surface with an LLM, runs scanners in parallel, **actively exploits** what it finds (multi-identity, out-of-band confirmed), triages away false positives, and writes a report — all as a durable, observable, retryable Conductor workflow. Point it at the **source code** or the **product docs** too, and it mines them to find and prove more.

> **⚠️ Authorized testing only.** Only test systems you **own or have explicit written permission to test**. Unauthorized scanning may be illegal; you are responsible for use. Scope and mutation limits are machine-enforced — see [Authorization & safety](#authorization--safety).

---

## Four ways to run it

| | `./sast` | `./scan` | `./assess` | `./swarm` |
|---|---|---|---|---|
| **Workflow** | `sast_report` | `security_scan` | `deep_assess` | `swarm_assess` |
| **Needs a live site?** | No — source only | Yes | Yes | Yes |
| **What it does** | Static analysis (semgrep/gitleaks/trivy) + route extraction → triage | Crawl → plan → active DAST → triage | Agentic pentest: understand → hypothesize → **exploit** → adversarially verify, over deepening passes | A round-based **team** of agents that share a blackboard and compose partial results into chained findings |
| **Confirms bugs live?** | No (triaged static findings) | Candidates + active DAST hits | **Yes** — re-runs each PoC, confirms blind bugs out-of-band, multi-identity | **Yes** — same oracles, at breadth; synthesizes chains no single agent sees |
| **Typical time** | 1–2 min | 3–5 min | tens of minutes | tens of minutes → hours (sized by `--rounds`/`--width`) |
| **Reach for it when** | You only have code | A fast surface pass | A thorough single-agent pentest | The deepest, widest hunt on a whole system |

`./run.sh <url> [flags]` is a convenience alias that forwards to `./scan`/`./assess`. Every mode writes the same [report bundle](#outputs) and obeys the same [authorization & capability gates](#authorization--safety).

---

## Quickstart

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make venv          # build the worker virtualenv (Python deps + Playwright Chromium) — once
make up            # start OWASP Juice Shop (a safe, intentionally-vulnerable target) on :3001
```

Scan it — `./scan` **auto-starts the whole Conductor stack** (server → register → worker fleet):

```bash
./scan http://localhost:3001 --authorized
#  → http://localhost:8080/execution/<id>     watch it live in the Conductor UI
#  → reports/<scan-id>/report.pdf + report.md + findings.json + report.sarif
```

`make down` tears it all down. Without `--authorized`, every mode stops at the authorization gate by design.

**Prerequisites:** `conductor` CLI, Docker, Python 3.11+, `jq`, `curl`. For a shared/CI stack, add `--no-bootstrap` and run `make server && make register && make workers` yourself. For Enterprise/Orkes, set `CONDUCTOR_SERVER_URL` / `CONDUCTOR_AUTH_KEY` / `CONDUCTOR_AUTH_SECRET` (or a gitignored `.env`) — see [docs/deployment.md](docs/deployment.md).

---

## Usage

### `./sast` — source only, no live target

Reads local files; no server, no `--authorized` needed.

```bash
./sast ./path/to/repo                       # fast triage (~1–2 min)
./sast ./path/to/repo --deep                # a code-reading agent judges each finding's reachability (fewer FPs)
./sast ./path/to/repo --target "acme@abc123" --no-bootstrap
```

SAST analyzers (`semgrep`/`gitleaks`/`trivy`) are used when present and degrade gracefully if absent. When the app is also running, prefer `./scan --source` or `./assess --source`, which fold the same static analysis into a live exploit-and-verify run.

### `./scan` — fast surface scan

```bash
./scan http://localhost:3001 --authorized                      # passive + active surface scan
./scan https://app.example.com --authorized \                  # authenticated (tests behind auth: IDOR/BOLA/SSRF)
  --auth-key "$KEY" --auth-secret "$SECRET" --token-url https://app.example.com/api/token --scope app.example.com
./scan https://app.example.com --authorized --source ./code --intrusive   # add SAST + route extraction
```

### `./assess` — deep single-agent pentest

```bash
# Two identities unlock BOLA / privilege-escalation / cross-tenant tests:
./assess https://app.example.com --authorized \
  --id 'userA=token:eyJ...A' --id 'userB=token:eyJ...B' --scope app.example.com

# Capability-2, docs + source + two tenants + purple team:
./assess https://your-system.example.com --authorized --capability 2 \
  --docs https://your-docs.example.com/ --source /path/to/source \
  --id 'orgA=key:K1,secret:S1,tokenurl:https://your-system.example.com/api/token,tenant:orgA' \
  --id 'orgB=key:K2,secret:S2,tokenurl:https://your-system.example.com/api/token,tenant:orgB' \
  --purple --require-tenants 2

# Hunt the source for chains, then dynamically confirm them live:
./assess https://app.example.com --authorized --capability 2 --source ./code --hunt \
  --id 'userA=token:...' --oob <collaborator>
```

A tiny seeded-vuln app ships in `examples/vuln-app/` (command/code/SQL injection, a no-authz endpoint, hardcoded secrets) — its routes aren't linked from `/`, so it's the quickest way to see `--source` route extraction work: `cd examples/vuln-app && npm install && node server.js`, then `./assess http://localhost:3000 --authorized --capability 2 --source examples/vuln-app`.

### `./swarm` — the collaborative deep hunt

The strongest mode. Instead of one agent, a **round-based team** works a shared **blackboard**: each round a standup plans a wave of independent agents — scouts for breadth, exploiters for depth, chainers, and adversarial refuters — then a reconcile step composes their verified primitives into **emergent chains no single agent sees end to end**. The whole swarm is one Conductor execution tree (the campaign `workflowId` is the `correlationId` on every child), so terminating the root is a one-shot kill switch.

It takes the same target/auth/scope/capability flags as `./assess`, plus `--width` (agents per wave, default 24), `--rounds` (default 4), and `--budget` (request budget). It loops until the surface is covered or a cap is hit.

```bash
# (a) The bundled Juice Shop demo (run `make up` first):
./swarm http://localhost:3001 --authorized --rounds 4

# (b) Add the target's SOURCE so scouts hunt code paths, not just the URL surface:
./swarm https://app.example.com --authorized --capability 2 --source ./code \
  --id 'userA=token:eyJ...A' --id 'userB=token:eyJ...B' --scope app.example.com

# (c) Full authenticated, multi-tenant campaign with DOCS + SOURCE (the strongest setup).
#     Use key/secret identities so tokens auto-refresh across a long campaign:
./swarm https://your-system.example.com --authorized --capability 2 \
  --docs https://your-docs.example.com/ --source /path/to/source \
  --width 24 --rounds 8 --budget 10000 \
  --id 'orgA=key:K1,secret:S1,tokenurl:https://your-system.example.com/api/token,tenant:orgA' \
  --id 'orgB=key:K2,secret:S2,tokenurl:https://your-system.example.com/api/token,tenant:orgB' \
  --oob <collaborator>
```

**What the inputs unlock** — this is what makes the swarm pentest a *system*, not a list of URLs:

- **`--docs <url|path>`** (repeatable; also loaded from a `--profile`) — ingests the product documentation and **learns how the system works**: its features, how to operate them, and the security *invariants* the docs claim ("only admins view audit logs", "secrets are masked", "a tag's grant is inherited"). Each documented endpoint and invariant becomes a tested hypothesis — *"prove the system does **not** enforce this"* — so agents weaponize features (define-and-run a workflow that SSRFs, register an integration that exfiltrates) instead of only probing endpoints. Scouts also query the docs live mid-campaign. JS-rendered doc sites are rendered headless.
- **`--source <path>`** — runs the SAST hunt and seeds code-grounded hypotheses (reachable sinks, weak authz) that agents confirm live; add `--hunt` to drive source-found chains to confirmation.
- **Two+ `--id` identities with distinct `tenant:` markers** — unlock the cross-identity oracles (BOLA / privilege-escalation / cross-tenant isolation).
- **`--oob <collaborator>`** (`make oob`) — confirms *blind* SSRF/RCE/exfil. **`--capability 2`** (with `make codeexec-image`) is what lets agents create-and-run workflows/integrations, required for state-changing feature abuse.

**What keeps it safe and trustworthy at breadth:** only a deterministic verifier that re-issues an oracle promotes a finding (oracles span SSRF, arithmetic RCE, time-blind SQLi, stored-XSS-in-victim-context, race invariants, path traversal, auth-state), and every candidate also faces an adversarial refuter. A global throughput ceiling on the `http_request`/`code_exec` task defs bounds live-target load at any tree width, and the root terminates the whole campaign on any policy/budget breach. Cost scales with wave width, so size it deliberately — `bench/swarm_eval.py` measures confirmed-findings-per-dollar vs. width; raise width only while that curve climbs. Design and rationale: [`design/SWARM.md`](design/SWARM.md).

---

## Authentication

The harness carries a credential you supply on every request — it never automates the IdP login itself.

| Target auth | How to supply it |
|---|---|
| API key / service account | `--id 'u=key:<K>,secret:<S>,tokenurl:https://app/api/token'` — re-exchanged locally each run (best for long campaigns; `./scan` uses `--auth-key/--auth-secret/--token-url`) |
| Bearer / JWT you already have | `--id 'u=token:<JWT>'` (or `--auth-token` for `./scan`); add `header:`/`scheme:` to override `Authorization: Bearer` |
| SSO (Google/Okta/SAML) | `./sso-capture https://app --label userA` — log in once in a real browser; it writes a credential file you pass as `--id 'userA=session:state/sessions/userA.json'` |

SSO access tokens expire — prefer API-key credentials for long runs. More in [docs/authentication.md](docs/authentication.md).

---

## Authorization & safety

`--authorized` synthesizes a minimal, capability-bounded manifest for quick tests and CI. For real engagements use `--manifest <file>` (approvers, in-scope hosts, window, capability ceiling, rate/data budgets, allowed techniques, forbidden operations/protected records) — it is validated at startup and **fails closed**.

**Capability levels gate every action.** The ceiling comes from the manifest (`--capability <0-4>`, default **1**); every worker refuses an action above it, and the harness can never raise its own level.

| Level | Permits | Verbs / actions | Mutates target? |
|---|---|---|---|
| **0** | Passive observation | recon only | No |
| **1** *(default)* | Reversible low-volume probes | `GET`/`HEAD`/`OPTIONS` | **No — writes & `code_exec` refused** |
| **2** | State-changing tests with synthetic data; feature exploitation | `POST`/`PUT`/`PATCH`/`DELETE`, `code_exec` | Only its own `sc-pentest-<run>-` objects, ledgered + auto-cleaned |
| **3** | Sensitive / risky proof | as L2, just-in-time approved | As L2 (sensitive scope) |
| **4** | Destructive / availability / real-data | — | **Prohibited by default; cannot self-escalate** |

For a **guaranteed read-only** run, keep `--capability 1` (writes and `code_exec` are refused at the gate). The availability / denial-of-wallet tier is off unless you pass `--resilience`. An independent safety governor halts the campaign on window expiry, a `--kill-switch` file, or a rate/budget/policy breach, and a tamper-evident audit log records every action. Details: [docs/authorization.md](docs/authorization.md).

---

## Outputs

Every run writes to `reports/<scan-id>/`:

| File | Contents |
|---|---|
| `report.pdf` / `report.md` | Human-readable findings report |
| `findings.json` | Structured findings — severity, CWE, OWASP, reproduction |
| `report.sarif` | SARIF 2.1.0 — ingest into GitHub code scanning, DefectDojo, or a SIEM |
| `dossier.json` | (`./assess` / `./swarm`) attack graph + confirmed/blind findings + residual-risk statement |

---

## Architecture

```
security_scan        normalize → authorization_gate → [source? → SAST → seed]
                       → recon + crawl + API discovery (FORK_JOIN)
                       → plan → active_scan (FORK_JOIN_DYNAMIC) → triage → report → PDF

swarm_assess         authorization_gate → seed blackboard
                       → understand once (recon → app model → DOCS INGEST → feature inventory → SAST hunt → personas)
                       → DO_WHILE rounds:  safety_check → standup → wave (FORK_JOIN_DYNAMIC) → reconcile
                       → scribe rollup (coverage + confirmed findings + spend)
```

LLM reasoning runs in native `LLM_CHAT_COMPLETE` tasks (Anthropic); security tools run in Dockerized workers; surface-gathering runs concurrently. More in [docs/architecture.md](docs/architecture.md).

| Deployment mode | Command | Notes |
|---|---|---|
| Host workers | `make workers` | All task types; scanners degrade gracefully if absent |
| Dockerized DAST | `make dast` | nuclei/sqlmap/dalfox/ffuf in a container |
| Dockerized SAST | `make sast SRC_ROOT=<dir>` | semgrep/gitleaks/trivy; source mounted read-only |
| Persistent browser | `make chrome` + `SC_CDP_URL=…` | one live browser, preserves SPA state across agent steps |
| Vector-RAG | `make rag` | pgvector-backed remediation retrieval for triage |

---

## Project layout

| Path | Contents |
|---|---|
| `conductor/` | Workflow JSON, task defs, `register.sh`, `ensure-stack.sh` (auto-bootstrap) |
| `workers/` | Task workers — `common/`, `recon/`, `swarm/`, `main.py`, Dockerfiles |
| `prompts/` | LLM system prompts (injected at register time) |
| `catalog/` | Security-objective catalog + substrate pack |
| `bench/` | Benchmark/oracle harness → `reports/BENCH.md` |
| `sast` · `scan` · `assess` · `swarm` | The four run entry points |
| `sso-capture` | Capture an SSO browser session → an `--id` credential |

## Documentation

`SKILL.md` is a concise procedural guide for AI assistants; the [docs site](docs/index.md) splits the operating model into task pages: [quickstart](docs/quickstart.md) · [scan vs assess](docs/scan-vs-assess.md) · [authorization](docs/authorization.md) · [authentication](docs/authentication.md) · [deployment](docs/deployment.md) · [architecture](docs/architecture.md) · [outputs](docs/outputs.md).

## Why Conductor

Security scans are long-running, parallel, and failure-prone — exactly what Conductor was built to orchestrate. See the [parent README](../README.md#why-conductor).
