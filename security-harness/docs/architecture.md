# Architecture

The harness is a Conductor workflow that runs a closed adversarial loop over a target. This page covers the workflow shape, how LLM reasoning and tooling are split, the Security Objective Catalog that drives everything, and the layered subsystem model.

## Workflow shape

```text
WORKFLOW security_scan
  normalize_target → authorization_gate (refuse if not authorized)
    → [source? → SAST sub-workflow → seed targets]
    → recon (passive) → web_crawl (Playwright + agent)
    → plan (LLM) → active_scan (FORK_JOIN_DYNAMIC)
    → triage (LLM: dedupe, severity, CWE/OWASP, cut FPs)
    → report_md (LLM) → report_pdf (GENERATE_PDF) → persist
```

LLM reasoning runs in native `LLM_CHAT_COMPLETE` tasks (Anthropic). Security tools run in Dockerized workers. Surface gathering (recon, crawl, API discovery, SAST) runs concurrently in a `FORK_JOIN` — the three sequential LLM steps (plan → triage → report) are on a true data dependency and cannot overlap.

The fast `security_scan` workflow above is the surface scan; `deep_assess` layers the multi-pass agentic loop on top of the same building blocks. Conductor's `DO_WHILE` bounds the iterative-deepening passes, `FORK_JOIN_DYNAMIC` fans out active checks, and `GENERATE_PDF` renders the final report — all durable and retryable. See [Scan vs Assess](scan-vs-assess.md).

## The closed adversarial loop

At its core the harness is an OODA / scientific-method loop run by an LLM agent with real tools. UNDERSTAND (build the model) is the entry bookend and REPORT is the exit bookend; between them the iterated core — HYPOTHESIZE → EXPLOIT → VERIFY → REFLECT — runs once per pass.

```text
UNDERSTAND ─▶ HYPOTHESIZE ─▶ EXPLOIT ─▶ VERIFY ─▶ REFLECT ─▶ REPORT
```

| Phase | Question it asks |
|---|---|
| **Understand** | What is this, how is it meant to work, what could go catastrophically wrong? |
| **Hypothesize** | What falsifiable attacks, tied to objectives & personas, are worth trying? |
| **Exploit** | Does the attack actually work — using the app's own features? |
| **Verify** | Is this real, or am I fooling myself? |
| **Reflect** | What's still untested; what does a confirmed finding unlock? |
| **Report** | What did we prove, what's the residual risk, would we notice? |

The loop is multi-pass and goal-directed. REFLECT closes two feedback edges into the next pass's HYPOTHESIZE:

- **Coverage gaps** — steer toward untested/blocked objective cells, not random surface.
- **Chaining** — a *confirmed* finding becomes a new precondition: its output (a credential, a reachable internal service, a tenant id, pivot material) seeds deeper, multi-step hypotheses, and the attack graph is assembled incrementally across passes. This is what makes the harness pursue a *kill chain*, not a flat list of independent findings.

A campaign ends when any of: the pass budget (`max_passes`) is reached; coverage saturates; the token/request budget is exhausted; or the safety governor halts the run. Termination is guaranteed.

## The spine: the Security Objective Catalog

The single most important design decision is that the "$1M list" of what-can-go-wrong is **data, not code** — a versioned catalog (`catalog/objectives.yaml`). It is a *spine* because one entry simultaneously drives five subsystems: `applicable_when` → scoping, `how_to_test` → hypothesis seeding, `required_identities` → adequacy gate, `impact_evidence` → the verifier's evidence bar, and `owasp/asvs/cwe` → coverage + compliance mapping.

Adding a vulnerability class is a data change plus a few localized hooks, not a redesign. The ~30 objectives group into these families:

| Family | Objectives (examples) | Focus |
|---|---|---|
| **CONF** — confidentiality / tenancy | cross-tenant read, BOLA cross-user, excessive data, sink leak, enumeration, stale access | Reading data you shouldn't |
| **INTEG** — integrity | cross-write, mass-assignment privesc, financial tamper, workflow-state, replay, audit tamper | Writing/altering data you shouldn't |
| **AVAIL** — availability (resilience tier) | complexity, ReDoS, rate-limit, denial-of-wallet, fail-open | Bounded abuse-of-cost / stability |
| **INFRA** — infrastructure / secrets | SSRF, RCE/injection, secret surface, path traversal, supply-chain CVE | Reaching internals / executing code |
| **AUTH** — identity / session | account takeover, session revoke, JWT flaw | Subverting authentication |
| **CRYPTO** | predictable tokens / weak crypto | Guessable ids, forged signed URLs |
| **AUTHZ** — authorization breadth | function-level (vertical) escalation, cross-interface, negative-space | Missing / inconsistent authz |
| **CLIENT** — client-side | XSS / CSRF / CORS | Script execution in a victim browser |
| **DETECT** — detection / response (purple) | detection coverage | Would we notice the attack? |

## Layered architecture

The system is seven cooperating subsystems, each with a narrow contract so an extension lands in exactly one layer.

| Layer | Concern | Contents |
|---|---|---|
| **L7 Assurance** | "what do we know & trust?" | coverage ledger, cross-run memory, dossier, benchmark, compliance |
| **L6 Safety / Authz** | "are we allowed?" | manifest, capability gate, halt, kill-switch, safety governor, cleanup |
| **L5 Epistemics** | "is it true?" | adversarial verify, evidence bars, OOB confirmation, provenance |
| **L4 Action** | "the hands" | `http_request`, `code_exec`, browser, `load_probe`, SAST, OOB |
| **L3 Reasoning** | "the judgment" | hypothesize, exploit, verify, reflect, triage, report (LLM tasks + prompts) |
| **L2 Knowledge & adapters** | "what is it, how does it work?" | surface, app model, docs ingestion, source/SAST, deps→CVE, profiles |
| **L1 Orchestration** | "run the loop" | Conductor server + worker fleet + the workflows (single entry point, proven to terminate) |

LLM seams (L3) are single workflow tasks carrying a system prompt from `prompts/*.md` — the prompts carry the *method*, the catalog and models carry the *content*. The action layer (L4) is capability-gated and halt-aware: `code_exec` runs in a hardened sandbox, the browser is JS-capable Playwright, and `oob` is an out-of-band collaborator for blind confirmation.

## Worker layout

Workers are external processes that poll Conductor by task name. They are organized by capability:

| Worker group | Responsibility |
|---|---|
| `recon/` | Passive reconnaissance and fingerprinting |
| `browser/` | Playwright crawl, login, and in-page actions |
| `dast/` | Active dynamic scanners (nuclei/sqlmap/dalfox/ffuf) |
| `sast/` | Static analysis, secret and dependency scanning (semgrep/gitleaks/trivy) |
| `api/` | API discovery and route extraction |
| `codeexec/` | Hardened sandbox for exploit code execution |
| `oob/` | Out-of-band collaborator for blind SSRF/RCE/exfil confirmation |
| `rag/` | Vector-RAG retrieval over the remediation knowledge base |
| `safety/` | Capability gate, safety governor, cleanup |
| `common/` | Shared scope, findings, SARIF, and voting logic |

See [Deployment modes](deployment.md) for how these run on the host vs in containers.
