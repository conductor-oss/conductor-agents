# Roadmap: from "scanner" to a $1M-grade assurance harness

**Goal.** Make the harness deliver what a customer paying ~$1M/engagement expects: a
defensible answer to *"can a determined adversary achieve a business-catastrophic
outcome, what is our residual risk, and would we even know?"* — measured against a
known standard, with honest coverage and reproducible proof.

This roadmap turns the gap analysis into trackable epics. Status legend per task:
`[ ]` not started · `[~]` in progress · `[x]` done.

> **See also:** `design/ARCHITECTURE.md` is the end-state design; `design/IMPLEMENTATION_PLAN.md`
> tracks the delta from that design to the current build (Phases 0–5, with the §19
> hill-climbing meta-loop as the centerpiece).

---

## Design principles (the spine)

1. **Catalog-as-spine.** A versioned **Security Objective Catalog** (data) drives
   hypothesis generation, coverage, reporting, and the benchmark. Adding a class =
   adding catalog entries + a capability to test them + a per-class verifier + a
   benchmark fixture. Everything else hangs off this.
2. **Applicable ≠ tested ≠ not-tested.** Catalog entries carry `applicable_when`
   predicates over the `app_model`; the report distinguishes N/A from untested.
3. **Identity adequacy is a precondition.** The harness validates the supplied
   identity *matrix* (tenants × roles × anon) and refuses to claim isolation coverage
   it cannot earn (fail-loud, like the auth fix).
4. **Per-class authorization.** The manifest gains `allowed_classes` + capability
   tiers (incl. a `resilience` tier); the safety governor enforces them.
5. **Per-class evidence bars** in verification (data contrast / measured knee / OOB
   hit / invalid-session) — no generic "got a 200".
6. **Reuse infra as sensors.** The egress allow-list proxy and OOB collaborator
   become data-exfil / third-party-leak / SSRF sensors.
7. **Safe-by-construction resilience.** Measure the derivative of latency/error vs
   bounded load; abort at thresholds via the halt governor. Never cause an outage.
8. **Per-class benchmark.** Seeded fixtures cover every catalog class; per-class
   recall/FP is the trust metric that justifies coverage claims.

---

## E0 — Security Objective Catalog (FOUNDATION; everything depends on this)

**Goal.** Encode the $1M list as structured data that drives hypothesize + coverage +
report + benchmark.

**Schema** (`catalog/objectives.yaml`, one entry per objective):
```yaml
- id: BOLA-CROSS-TENANT
  class: confidentiality            # confidentiality|integrity|availability|infra|auth|authz|tenancy|logic|crypto|client|detection
  objective: "Read another tenant's data"
  invariant: "A tenant principal cannot read/list/search another tenant's objects"
  applicable_when: "multi_tenant"   # predicate over app_model facts
  required_identities: "2+ distinct tenants"
  required_capability: 1            # 1 read .. 2 write .. resilience
  how_to_test: "as tenantA request tenantB object ids across every interface/protocol"
  impact_evidence: "response contains tenantB classified data (cross-identity contrast)"
  coverage_dimension: tenant_isolation
  refs: { owasp: "A01:2021", asvs: "V4.2", cwe: "CWE-639" }
```

**Deliverables / tasks**
- [x] `catalog/objectives.yaml` seeded with the full taxonomy (31 objectives across 11 classes).
- [x] `workers/common/catalog.py`: `load()`, `derive_facts()`, `applicable()`, `applicable_entries()`/`na_entries()`, `for_class()`, `coverage_dimensions()`, `compact()`.
- [x] `app_model` emits structured **facts**; `catalog_context` worker derives facts (explicit + heuristic) and computes applicable vs N/A objectives.
- [x] `prompts/hypothesize.md` consumes the **applicable** catalog entries (breadth mandate) and tags each hypothesis with `objective_id`, which flows through to confirmed findings.
- [x] `workers/common/coverage.py` `build_from_catalog()` builds cells **from the catalog** and classifies tested/partial/untested/**not_applicable**; wired into the in-loop + post-loop coverage builds.
- [x] `prompts/report.md` Coverage section renders catalog coverage with OWASP/CWE refs and excludes N/A from the denominator.
- [x] Tests (`tests/test_catalog.py`, extended `test_coverage`) green; workflows register cleanly.

**Acceptance.** A scan against the seeded app reports coverage as "X of N *applicable*
catalog objectives tested," with N/A objectives excluded and a CWE/ASVS column. Unit
tests for `catalog.applicable()` and catalog→coverage wiring.

---

## E1 — Multi-tenant isolation as a first-class guarantee  *(Phase A)*

**Goal.** Make the single highest-value SaaS class rigorous, and *demand* the identities to test it.

- [x] **Identity-adequacy gate** (`workers/common/identity.py` + `normalize_target`): classifies identities into a matrix (authenticated/cross_user/cross_tenant/privesc); emits `identity_adequacy`. Coverage now marks isolation objectives **blocked** (not untested/clean) when the required identities are absent; a confirmed finding still overrides to tested.
- [x] CLI: `--id ...,tenant:<id>` to tag tenants (carried through `resolve_identities`); `--require-tenants N` fails fast; warns that cross-tenant is BLOCKED with <2 tenants.
- [x] Report/dossier surface `identity_adequacy` + the `blocked` coverage status; `cross_tenant_testable` driven by adequacy.
- [ ] **(carry-over)** Systematic cross-identity *matrix engine* (auto ownerA vs tenantB vs anon contrast per discovered object-id) — currently driven by the LLM exploit agent (personas + catalog); a deterministic sweep is a follow-on within E1.

**Acceptance.** Met for the blocking guarantee (unit-tested: 1 identity ⇒ cross-tenant
`blocked`, confirmed finding ⇒ `tested`). Deterministic matrix sweep remains a follow-on.

---

## E2 — Data-flow / DLP / data-classification  *(Phase B)*  ← your #1, deepened

**Goal.** Answer "where does my data go, and can someone see data that isn't theirs?"
as a *tracing* problem.

- [ ] **Data classifier** (extend `workers/common/sensitive.py`): classify response/field
  data into `pii|phi|pci|secret|token|financial|internal` with counts (not just secrets).
- [ ] **Tagged-canary data-flow**: seed uniquely-tagged synthetic PII as identity A
  (`sc-pii-<runid>-…`), then sweep sinks for the tags — other identities' responses,
  exports/reports/PDF, GraphQL over-fetch, error messages, and **third-party egress
  observed by the codeexec egress proxy** (turn the proxy into a logging sensor).
- [ ] **Excessive-data-exposure check**: diff API JSON fields vs what the UI/role needs.
- [ ] New objective classes + coverage cells; verifier evidence bar = "tag belonging to A surfaced to B / to a sink / to a third party."

**Acceptance.** Seeded app: a tagged PII record planted as userA is detected leaking to
userB and to an export endpoint; third-party egress of a tag is flagged from proxy logs.

---

## E3 — Resilience / availability / denial-of-wallet  *(Phase D — new capability tier)*  ← your #2

**Goal.** Safely find ways to destabilize or run up cost, without causing an outage.

- [ ] **New capability/class** `resilience` in `authz.py` + manifest `allowed_classes`; gated, opt-in, off by default. Safety governor enforces.
- [ ] `workers/load/` worker: bounded, ramped concurrency measuring the **derivative** of latency/error vs load; **abort thresholds** wired to `halt.py` (stop at first sign of degradation — find the knee, don't push past it).
- [ ] Checks: unbounded/expensive queries, GraphQL complexity/depth, ReDoS inputs, large-payload/decompression, pagination amplification, missing rate-limits, **account-lockout-as-DoS**, OTP/email bombing, **fail-open under dependency stall**, **denial-of-wallet** (egress/LLM/compute cost amplification).
- [ ] Evidence bar = measured latency/error inflection within the bounded envelope, never sustained outage.

**Acceptance.** Seeded app with an O(n²) endpoint: harness reports the complexity knee
with the request that triggers it, and *self-aborts* before degradation — verified by the
governor halt log.

---

## E4 — Infrastructure & secret extraction depth  *(Phase B)*  ← your #3, deepened

**Goal.** Leak the underlying system: infra secrets, credentials, internal reach, RCE.

- [ ] **SSRF depth**: cloud metadata (IMDS v1/v2), internal services, secret managers, K8s API — confirmed via OOB + response signals.
- [ ] **Injection/exec depth**: deserialization, SSTI, expression/code-eval features, command injection — beyond the current single probes.
- [ ] **Secret-surface sweep**: `.git`/`.env`/backup/source-map/debug endpoints, config endpoints, stack-trace leakage; downstream creds (DB strings, S3/payment/LLM keys, SA tokens, webhook secrets).
- [ ] **Path traversal / LFI** → source/keys/`/etc/*`.
- [ ] **Supply chain**: surface known-CVE deps (extend SAST/trivy wiring into findings + dependency confusion signals).
- [ ] **Blast-radius / internal reach** report from the egress proxy (what the app/sandbox *tried* to reach).

**Acceptance.** Seeded app with an SSRF-to-metadata and a hardcoded downstream key:
both confirmed (OOB hit + extracted-credential evidence), blast radius noted.

---

## E5 — Identity, session & crypto depth  *(Phase C)*

- [ ] **Auth/identity checks**: account-takeover chains (reset poisoning, OAuth `redirect_uri`/`state`/IdP confusion, MFA bypass, magic-link/OTP), enrollment/recovery/identity-linking abuse.
- [ ] **Session/token**: revocation *actually* invalidates (temporal test), concurrent-session policy, **JWT flaws** (alg/`none`, weak secret, `kid` injection, no-expiry), SSO/SAML signature-wrapping/audience confusion, API-key scoping & rotation (old/leaked keys still work?).
- [ ] **Crypto**: predictable tokens/IDs (randomness tests), signed-/capability-URL weaknesses, TLS config.

**Acceptance.** Seeded app: a revoked session still works → confirmed temporal finding;
a JWT `alg:none` accepted → confirmed.

---

## E6 — Authorization consistency & negative-space  *(Phase B; deepens existing)*

- [ ] **Cross-interface consistency**: same operation across REST / GraphQL / UI / import / mobile / v1-vs-v2 — flag where one path enforces and another doesn't.
- [ ] **Field-level** (mass-assignment) and **function-level** (hidden admin ops) systematically.
- [ ] **Negative-space detector**: operations that return success with *no* observable authz decision.
- [ ] **Delegated-authority/sharing abuse**: share-to-self escalation, public-link guessing, confused deputy.

**Acceptance.** Seeded app where REST enforces but GraphQL doesn't → the inconsistency is confirmed.

---

## E7 — Business-logic & economic-abuse modeling  *(Phase C)*

- [ ] Strengthen docs→invariant extraction; add an **economic-abuse** objective family (pricing/discount/trial/referral/credit/marketplace/payout fraud, anti-automation).
- [ ] Domain-invariant catalog entries are **profile/customer-suppliable** (per-engagement rules file), feeding hypothesize + coverage.

**Acceptance.** Given a supplied invariant ("trial limited to 5"), the harness attempts and reports a bypass or a clean negative.

---

## E8 — Detection / response / forensics (purple) depth  *(Phase E; deepens opt-in purple)*

- [ ] Per confirmed attack, score **prevented / logged / alertable / attributable / reconstructable / fail-safe**, grounded in observable signals (block status, exposed audit/event endpoints).
- [ ] **Audit-log integrity** probe (tamper-evidence) where the app exposes logs.
- [ ] Detectability rolls into the dossier as a control-assurance section.

**Acceptance.** A confirmed write that produces no observable audit entry is reported as a *detection gap*, not just a vuln.

---

## E9 — Assurance & compliance deliverables  *(Phase E)*

- [ ] **ASVS level** computed from catalog coverage; SOC2/PCI/HIPAA/GDPR-relevant control mapping in the dossier.
- [ ] **Regression suite** export: each confirmed finding → a re-runnable test; `--retest <dossier>` mode to verify remediation.
- [ ] **Residual-risk + coverage attestation** upgraded to per-class, per-applicability.

**Acceptance.** Dossier shows "ASVS V-level: target met X/Y", a regression bundle is emitted, and `--retest` re-verifies prior findings.

---

## E10 — Per-class benchmark & harness self-assurance  *(Phase E; continuous)*

- [x] Canonical coverage corpus (`bench/expected/catalog-coverage.json`) covers **all 31** catalog objectives across every class (0 unmeasured) + an adversarial near-miss corpus (9 negatives / 4 subtle positives) + a known-clean app row.
- [x] `bench/coverage.py` reports **oracle coverage + per-class fixture inventory + held-out split** → `reports/BENCH.md` **offline** (`make bench`); `bench/run.py` (`make bench-live`) appends **live per-class recall / FP**.
- [x] Gate: `tests/test_bench_report.py` fails if a catalog objective lacks a fixture (so a class can't be advertised "covered" without a benchmark fixture); HC must not auto-tune any unmeasured class.
- [x] Per-target seeded ground truth for **≥2 scored targets** — `seeded-vuln-app` (SAST, `--source`) + `juice-shop` (black-box, `bench/expected/juice-shop.json`, 13 documented challenges across 7 classes). `holdout.k=2` is now a genuine k-fold split and `oracle_adequate` is True. *(Live recall on juice-shop is appended by `make bench-live` against a reachable instance; the seeded vuln-app remains the SAST-scored anchor.)*

**Acceptance.** `make bench` prints the oracle-coverage scorecard offline (31/31 measured today); `make bench-live` adds per-class recall when a server + ground-truthed targets exist.

---

## Phasing & dependencies

| Phase | Epics | Theme | Rationale |
|---|---|---|---|
| **A — Foundation** | E0, E1 | catalog spine + multi-tenant rigor + identity gate | Everything measurable; fixes the biggest current blind spot (single-identity false-clean). |
| **B — Crown jewels** | E2, E4, E6 | data-leak/DLP, infra/secret extraction, authz consistency | Highest-impact attacker objectives. |
| **C — Identity & logic** | E5, E7 | auth/session/crypto, business/economic abuse | Deep classes scanners can't do. |
| **D — Resilience** | E3 | availability / denial-of-wallet | New gated capability tier; deliberate, riskier. |
| **E — Assurance** | E8, E9, E10 | purple depth, compliance/ASVS/regression, per-class benchmark | What turns findings into a $1M deliverable. |

**Hard dependency:** E0 precedes all (it's the spine). E1's identity facts feed E2/E5/E6.
E10 should grow alongside each epic (add a fixture as each class lands).

## Definition of done (per epic)
1. Catalog entries for the class (E0 schema). 2. The capability/worker to test it.
3. Per-class verifier evidence bar. 4. Coverage + report + dossier wiring.
5. A seeded benchmark fixture proving detection (E10). 6. Unit tests green; one live
validation against the seeded app.

## Tracking
This file is the source of truth (checkboxes per task). The in-session task tracker
mirrors the **epics** (E0–E10) with phase dependencies so progress is visible while we
implement. Update the `[ ]/[~]/[x]` marks as tasks land.
