# security-conductor — Architecture & Design

> **What this document is.** The single source of truth for *what the harness is, how its
> parts compose, and why it is shaped this way.* It describes the **end-state architecture** —
> the system as designed — naming its components and their contracts, the core data model,
> the load-bearing decisions, and the extension seams along which it grows.
>
> **What it is not.** It is not the requirements wishlist (`SECURITY_HARNESS_SPEC.md`), not
> the orchestration termination proof (`docs/EXECUTION_MODEL.md`), and not the backlog or a
> status report (`docs/ROADMAP.md`). It describes the target design, not what is built today;
> the delta between this design and the current implementation is tracked separately.
>
> **Units.** A **campaign** (≡ a *run*) is one invocation of `assess(I)`. A **pass** is one
> iteration of the assessment loop within a campaign. An **epoch** is one self-improvement
> step (§19) taken over a batch of many campaigns' traces. **Layers** are written `L1`–`L7`
> (§7); the loop-engineering *levels* of §19 are written `Level 1`–`Level 4`.

---

## 1. The question the harness answers

A scanner answers *"which signatures matched?"* This harness is built to answer a
**strategic, assurance-grade** question:

> *Can an adversary — at a stated level of access — achieve a business-catastrophic
> outcome against this target; what is the residual risk; and would anyone notice?*

Everything in the design serves that question. The three catastrophic outcomes it is
organized around are generic, not target-specific: **(1) data leak / cross-tenant
exposure, (2) destabilization / denial-of-availability, (3) infrastructure-secret
extraction**. Each has a worked-vertical *chain* (§13, §16, §15); the *"would anyone
notice?"* clause has its own assessment (§17). The Security Objective Catalog (§6) is the
concrete encoding of "what catastrophic looks like," and the assessment loop (§5) is the
engine that pursues it.

---

## 2. Design principles

These are the invariants the rest of the design is accountable to. When a change is
proposed, it is checked against these first.

1. **Generic by construction, specialized by data.** The engine knows nothing about any
   one product. All target-specific knowledge lives in *data* — a profile, a manifest, an
   ingested model — never in the core. (A harness that runs *on* Conductor must still be
   able to test *anything*.)
2. **Adversarial, not confirmatory.** Verification is a separate agent whose job is to
   *refute* a finding. A finding survives only by surviving an attempt to kill it. False
   positives are treated as more expensive than misses.
3. **Honest over impressive.** "Attempted, not confirmed" and "not assessed" are
   first-class outcomes. The system never upgrades absence-of-evidence into
   evidence-of-absence.
4. **Safe by construction.** Authority is explicit and machine-enforced (a manifest, not a
   boolean). Capability is gated per action. The campaign can halt itself. Nothing
   state-changing happens at a capability level the operator did not grant.
5. **Orchestrated, not scripted.** The campaign is a Conductor workflow with a single
   entry point and a proven termination property (see `EXECUTION_MODEL.md`). Control flow
   is deterministic; *judgment* is delegated to LLM tasks at well-defined seams.
6. **Knowledge compounds.** Findings, coverage, and tried hypotheses persist across runs,
   keyed to the deployment. A second run is not a cold start.
7. **Every assertion has provenance.** Each claim is tagged observed / documented / from
   source / inferred, with a confidence and a timestamp. This is what makes contradiction
   detection and trust possible.

---

## 3. System context — two planes

The harness operates across two strictly separated planes. Conflating them is the most
common source of confusion, so the design keeps them explicit.

```
        OPERATOR  (human or CI/release trigger → ./assess CLI + a manifest of authority)
            │  start(deep_assess, I)
            ▼
┌────────────────────────────────────────────────┐         ┌────────────────────────────┐
│  ORCHESTRATION PLANE                             │  acts   │  TARGET PLANE              │
│  a local Conductor server + worker fleet         │ ──────▶ │  the system under test     │
│  (runs the loop, holds no target authority       │ ◀────── │  (app URL, its APIs,       │
│   beyond what the manifest grants)               │ observes│   its docs, its source)    │
└────────────────────────────────────────────────┘         └────────────────────────────┘
```

- **Orchestration plane** = where the campaign *runs* (a Conductor server + the worker
  fleet). It is infrastructure. It is incidental that the reference target is also
  Conductor; the harness is not a Conductor pen-test tool.
- **Target plane** = the system under test, supplied entirely as **input** (`I` in
  `EXECUTION_MODEL.md`): app URL, identities, optional source path, optional docs.

The only ingress is `assess(I)`; there is no side channel, so a run is fully determined by
its input vector plus what it observes from the target. An *event-driven* trigger (a CI job
or release webhook — §19, Level 3) is simply an automated caller of this same ingress, not a
second way in.

---

## 4. Inputs to the harness

A run is **fully determined by its input vector `I`** plus what the harness observes from
the target — there is no side channel (§3). `I` falls into two groups: the **knowledge
inputs** that describe *the target and how it is meant to work*, and the **governing inputs**
that set authority and identity. This section specifies the knowledge inputs, which are the
substance the assessment reasons over.

### 4.1 The three knowledge inputs

```
        ┌──────────────────────────────────────────────────────────┐
        │  what is it &       │  how is it meant   │  what is the      │
        │  how is it built?   │  to be used?       │  running system?  │
        │                     │                    │                   │
        │  SOURCE LOCATIONS   │  DOCS              │  APP URL          │
        │  (local paths)      │  (files or URLs)   │  (live endpoint)  │
        └─────────┬───────────┴──────────┬─────────┴─────────┬─────────┘
                  ▼                       ▼                   ▼
            implementation          intended behavior     observed behavior
            truth (L2 adapters) → fused into the APP MODEL ← (L2 surface)
```

| Input | Form | Required? | What it gives the harness | Consumed by (L2 adapter) |
|---|---|---|---|---|
| **App URL** | a live endpoint (e.g. `https://app.example.com`) | **Yes** — the one substantive input a run cannot omit | the *running* system: reachable surface, observed behavior, the thing attacks are actually executed against; identities and the manifest attach to it | `surface` / browser crawl, `http_request` |
| **Source locations** | one or more local paths to the implementation | Optional | *implementation truth*: route/endpoint extraction, SAST findings, and dependency manifests → version-matched CVE leads | source/`sast`, `deps` → OSV/NVD/GHSA |
| **Docs** | a set of **files or URLs** that show how to use the app | Optional | *intended behavior*: documented workflows and invariants the app is supposed to uphold — the basis for business-logic and invariant-violation hypotheses | `docs_ingest` |

**App URL — the anchor.** The only knowledge input a run requires. Everything observed,
every action taken, and every finding is relative to this target. Credentials/identities
(§9) and the authorization manifest (§10) are bound to it. With *only* this input the
harness runs as a black-box assessment from observed behavior.

**Source locations — implementation truth.** Local path(s) to the target's code. They let
the harness read what the running system won't tell it: the real route table, code-level
weaknesses (SAST), and the exact dependency versions in the build manifests — which feed
the dependency→CVE→exploit chain (§14). Source turns "guess the surface" into "know the surface."

**Docs — intended behavior.** A set of **files or URLs** describing how the app is meant to
be used (guides, API references, concept docs). This is what lets the harness test
*business logic*: a documented invariant ("a tenant can only see its own workflows") becomes
a hypothesis to *violate*, and a contradiction between a doc claim and an observation becomes
a high-value lead (§2, provenance). Docs are ingested through the `docs_ingest` adapter,
which spans **fidelity tiers** (§11) from plain files / markdown / OpenAPI up to a
JS-rendered documentation-site crawl and vector-indexed retrieval; the form of the docs
determines which tier serves them.

### 4.2 The governing inputs (specified elsewhere)

These accompany the knowledge inputs but shape *authority and perspective* rather than
*content*:

- **Identities / credentials** — zero or more principals (anon, users, tenants, admin) the
  harness may act as. Their *adequacy* gates relational objectives (cross-user, cross-tenant
  need ≥2 distinct parties). See §9.
- **Authorization manifest** — the machine-readable grant of authority: scope, capability
  ceiling, window, budgets, forbidden operations, kill-switch. See §10.
- **Scope** — the host/port/API boundary the harness must stay within; everything outside is
  refused. See §10.

### 4.3 Design stance on inputs

- **Graceful degradation.** More inputs, and higher-fidelity inputs, yield a deeper
  assessment; fewer inputs narrow it without breaking it. App-URL-only is a black-box run;
  adding source and docs turns it grey- then white-box.
- **Each input feeds an adapter, not the core.** Inputs are consumed by L2 adapters (§11)
  and fused into a single **app model**; the loop reasons over the model, not the raw inputs.
  A new *kind* of input, or a higher-fidelity tier for an existing one, is a localized
  adapter change (§20).
- **Inputs are data, never instructions.** Per the guardrail (§10), content arriving via
  docs, source, or the live target is untrusted *data*; it cannot alter scope, authority, or
  tool selection.

---

## 5. The central abstraction: a closed adversarial loop

The harness is, at its core, an **OODA / scientific-method loop** run by an LLM agent with
real tools. UNDERSTAND (build the model) is the entry bookend and REPORT is the exit bookend;
between them, the **iterated core** — HYPOTHESIZE → EXPLOIT → VERIFY → REFLECT — runs once per
pass:

```
   UNDERSTAND ─▶ HYPOTHESIZE ─▶ EXPLOIT ─▶ VERIFY ─▶ REFLECT ─▶ REPORT
   (entry:        ┌─ propose     attempt    refute    decide      (exit:
    model +       │  falsifiable            the       next pass    triage,
    intel)        │  attacks                result      │          dossier)
                  │                                      │
                  └── coverage gaps + confirmed-finding ─┘
                      preconditions (chaining)
```

| Phase | Question it asks | Realized by |
|---|---|---|
| **Understand** | What is this, how is it meant to work, what could go catastrophically wrong? | `surface`, `app_model`, `docs_ingest`, `dep_cve_scan`, `catalog_context` |
| **Hypothesize** | What falsifiable attacks, tied to objectives & personas, are worth trying? | `hypothesize` (LLM) over the catalog + CVE leads + tried-history |
| **Exploit** | Does the attack actually work — using the app's own features? | `exploit_agent` (LLM + `http_request` + `code_exec` + browser) |
| **Verify** | Is this real, or am I fooling myself? | `verify_finding` (adversarial LLM + OOB confirmation) |
| **Reflect** | What's still untested; what does a confirmed finding unlock? | `reflect_pass` (LLM) over the coverage ledger |
| **Report** | What did we prove, what's the residual risk, would we notice? | `triage`, `dossier`, `report` (on exit) |

The loop is **multi-pass** and **goal-directed**. REFLECT closes two feedback edges into the
next pass's HYPOTHESIZE:

- **Coverage gaps** — steer toward untested/blocked objective cells, not random surface.
- **Chaining** — a *confirmed* finding becomes a new precondition: its output (a credential,
  a reachable internal service, a tenant id, pivot material) seeds deeper, multi-step
  hypotheses, and the **attack graph** (§8, §18) is assembled incrementally across passes
  rather than only at report time. This is what makes the harness pursue a *kill chain*, not
  a flat list of independent findings.

**Exit conditions.** A campaign ends when any of: the pass budget (`max_passes`) is reached;
coverage saturates (a pass yields no new tested cells and no new chain); the token/request
budget is exhausted; or the safety governor halts the run (§10). Termination is guaranteed
(`EXECUTION_MODEL.md`).

**The loop opens with an intel refresh.** Before UNDERSTAND reasons about the target, the run
pulls the *time-sensitive* external knowledge it depends on — the vulnerability feeds that
drive the CVE chain (§14) and the cloud/orchestrator metadata conventions that drive the
infrastructure chain (§15) — from their authoritative sources, and stamps each with an
`as_of` marker carried into the report. A campaign therefore reasons over *current*
vulnerabilities and *current* substrate conventions; the harness never uses a stale bundled
snapshot to confirm a finding.

---

## 6. The spine: the Security Objective Catalog

The single most important design decision is that the "$1M list" of what-can-go-wrong is
**data, not code** — a versioned catalog (`catalog/objectives.yaml`). The catalog is a
*spine* because one entry simultaneously drives five subsystems:

```
                      ┌───────────────────────────────────────┐
   one catalog entry  │  applicable_when  →  scoping            │
   (objective)        │  how_to_test      →  hypothesis seeding │
                      │  required_identities → adequacy gate     │
                      │  impact_evidence  →  verifier evidence bar│
                      │  owasp/asvs/cwe   →  coverage + compliance│
                      └───────────────────────────────────────┘
```

Objectives are grouped into classes spanning the three catastrophic outcomes and more —
confidentiality/tenancy, authz, integrity, availability, infra, auth, crypto, client, logic,
and detection. The worked-vertical chains (§13–§17) compose subsets of these objectives;
every other class runs through the same generic loop. Consequence for evolution: **adding a
vulnerability class is a data change plus a few localized hooks** (§20), not a redesign. The
catalog is the finite, auditable definition of "done enough."

---

## 7. Layered architecture

The system is seven cooperating subsystems. Each has a narrow contract, so an extension
lands in exactly one layer.

```
┌──────────────────────────────────────────────────────────────────────┐
│ L7  ASSURANCE      coverage · memory · dossier · benchmark · compliance │  "what do we
│                                                                         │   know & trust?"
├──────────────────────────────────────────────────────────────────────┤
│ L6  SAFETY/AUTHZ   manifest · capability gate · halt · kill-switch ·    │  "are we
│                    safety governor · guardrail · cleanup                │   allowed?"
├──────────────────────────────────────────────────────────────────────┤
│ L5  EPISTEMICS     adversarial verify · evidence bars · OOB · provenance│  "is it true?"
├──────────────────────────────────────────────────────────────────────┤
│ L4  ACTION         http_request · code_exec · browser · load_probe ·    │  "the hands"
│                    sast · oob                                           │
├──────────────────────────────────────────────────────────────────────┤
│ L3  REASONING      hypothesize · exploit · verify · reflect · triage ·  │  "the judgment"
│                    report  (LLM tasks + prompts)                        │
├──────────────────────────────────────────────────────────────────────┤
│ L2  KNOWLEDGE      surface · app_model · docs ingestion · source/SAST · │  "what is it,
│     & ADAPTERS     deps→CVE · profiles  ◀── ingestion fidelity tiers    │   how does it
│                                                                         │   work?"
├──────────────────────────────────────────────────────────────────────┤
│ L1  ORCHESTRATION  Conductor server + worker fleet + the workflows      │  "run the loop"
│                    (single entry point; proven to terminate)            │
└──────────────────────────────────────────────────────────────────────┘
```

- **L1 Orchestration.** Conductor workflows (`deep_assess` and its sub-workflows) provide
  deterministic control flow, fan-out/fan-in, loops with bounded iterations, and the
  termination guarantee. Workers are external processes that poll by task name; the
  *coverage predicate* (`EXECUTION_MODEL.md` §Def-3) — every SIMPLE task has a polling
  worker — is the precondition for a run to make progress.
- **L2 Knowledge & adapters.** Turns a raw target into a model the agent can reason over.
  This is an **adapter layer** with explicit *fidelity tiers* (§11) — the place where
  "ingest docs," "parse source," "enumerate surface," and "resolve dependencies" each plug
  in behind a fixed contract.
- **L3 Reasoning.** The LLM seams. Each is a single workflow task with a system prompt
  (`prompts/*.md`). Prompts carry the *method* (how to think like an attacker for this
  phase); the catalog and models carry the *content*.
- **L4 Action.** The agent's hands. `http_request` (capability-gated, halt-aware),
  `code_exec` (hardened sandbox — operate the product's own SDK/API), `browser`
  (JS-capable Playwright), `load_probe` (bounded resilience ramp), `sast`, `oob`
  (out-of-band collaborator for blind confirmation).
- **L5 Epistemics.** The truth machinery: a separate refuting verifier, per-class evidence
  bars, OOB blind confirmation, and provenance tagging on every assertion.
- **L6 Safety/Authz.** Authority and restraint, machine-enforced (§10). Not advisory.
- **L7 Assurance.** What converts a pile of findings into an answer: the coverage ledger,
  cross-run memory, the benchmark, the living dossier, compliance roll-up, and regression.

---

## 8. Core data model

The artifacts that flow between layers. These are the stable nouns of the system; keeping
them stable is what lets layers evolve independently.

| Artifact | Meaning | Produced by | Consumed by |
|---|---|---|---|
| **Input vector `I`** | target, identities, source, docs, manifest, scope | operator | `normalize_target` |
| **Manifest** | machine-readable grant of authority + capability ceiling | operator | safety/authz (L6) |
| **App model** | what the target is + how it's *meant* to work + facts | L2 | hypothesize, reflect, report |
| **Catalog facts** | which objectives are applicable / N/A for this target | `catalog_context` | hypothesize, coverage |
| **Persona** | an attacker identity + objective + start position + constraints | `personas` (derived) | hypothesize, exploit |
| **Hypothesis** | a falsifiable attack tagged with `objective_id` + persona + signature | hypothesize | exploit |
| **Finding** | a confirmed/rejected/inconclusive result + evidence + provenance + content-hash | verify | report, memory |
| **Coverage ledger** | per-objective status: tested / partial / untested / blocked / N/A | coverage | reflect, report |
| **Attack graph** | confirmed findings as nodes, chaining preconditions as edges | reflect (incremental) | report, dossier |
| **Deployment fingerprint** | stable key (host + version) for cross-run identity | `normalize_target` | memory |
| **Dossier** | the assurance deliverable: model + coverage + findings + attack graph + residual risk | dossier | report, memory |

Two cross-cutting fields appear on every assertion: **provenance**
(observed/documented/source/inferred) and a **signature** (`class|target|sorted(identities)`)
used for dedupe across passes and runs. A finding's **content-hash** is the basis of evidence
integrity (§10): the stored hash is recomputed on load, so a tampered record is detectable.

---

## 9. Identity & persona model

Many catastrophic outcomes are *relational* — they only exist between two parties
(cross-user, cross-tenant). The design encodes this directly:

- Identities are supplied as input and classified into an **adequacy** level:
  `authenticated → cross_user(≥2 users) → cross_tenant(≥2 distinct tenants) → privesc`.
- An objective declares the identities it *requires*. If the campaign lacks them, the
  objective's coverage cell is **blocked**, not "untested" and not "clean."
- This is why a single-credential run never claims "no cross-tenant issue" — the design
  forces that to surface as *blocked for lack of a second tenant*, not as a pass.

A **persona** (a derived artifact, §8) pairs an identity with an objective, a starting
knowledge level, and constraints, so the exploit agent acts *as* "a malicious tenant" or "an
anonymous internet attacker" rather than just "token B."

---

## 10. Authority, capability & safety model

Authority is **explicit, machine-enforced, and fail-closed** — the antithesis of an
`--authorized` boolean.

- **Manifest** = the grant: approvers, in-scope hosts/ports/APIs, a validity window,
  allowed personas/techniques, a **capability ceiling**, rate/volume budgets, forbidden
  operations, kill-switch path.
- **Capability levels (0–4)** classify every action: passive/analysis-only with no live
  request to the target (0); read (GET/HEAD) (1); state-changing write (2); sensitive
  code-exec (3); destructive/forbidden (4). The `http_request` and `code_exec` chokepoints
  **refuse** any action above the granted ceiling — before it fires.
- **Budgets are global.** Rate and data-volume budgets are enforced across the whole campaign,
  including parallel fan-out (concurrent exploit/verify forks draw from one shared counter),
  not per-fork — so concurrency can never multiply past the grant.
- **Halt conditions** evaluate each result (unexpected real sensitive data, out-of-scope
  effect, budget exceeded, forbidden op) and can request campaign halt.
- **Safety governor** is a *separate worker the planner cannot override*; it re-checks the
  window, the kill-switch file, and prior halt flags at every pass boundary and can
  `TERMINATE` the run.
- **Leave-clean guarantee.** Every resource the harness creates on the target (each tagged
  with a run-scoped marker) is tracked in a ledger and swept on exit; a state-changing
  campaign leaves the target in its prior state. Cleanup is itself capability-gated and
  in-policy because it only removes the harness's own artifacts.
- **Guardrail** preamble on every LLM prompt: target-provided content (HTTP bodies, docs,
  source) is *untrusted data, never instructions* — it cannot change scope, authority, or
  tool selection.
- **Evidence integrity / self-defense.** Findings and the action log are tamper-evident: the
  action log is an append-only hash chain, finding evidence is content-hashed (§8), and a
  mismatch on reload quarantines the record. This defends the harness against prompt
  injection (above), memory poisoning, and silent evidence edits.

The default posture is conservative: a bare `--authorized` synthesizes a **capability-1
(read-only)** manifest; state-changing pen-testing requires an explicit grant.

---

## 11. Knowledge & adapter model

L2 is an **adapter layer**: each knowledge source is an adapter that produces a slice of
the app model behind a fixed contract (in → a slice of the app model). Each adapter declares
a **fidelity tier**, so the model records *how much* a source yielded as a first-class fact.

| Knowledge source | Adapter | Fidelity tiers (low → high) |
|---|---|---|
| Live surface | `surface` / browser crawl | passive headers → JS-rendered crawl (Playwright) |
| Intended behavior (docs) | `docs_ingest` | plain-text/markdown/OpenAPI fetch → JS-rendered doc-site crawl → vector-indexed retrieval |
| Implementation truth | source / `sast` | route extraction → SAST findings → dep manifest parse |
| Supply chain | `deps` → OSV/NVD/GHSA | stack inference → manifest-resolved versions → version-matched CVE |
| Target-specific quirks | `profiles` | generic defaults → a profile (auth scheme, token exchange, cleanup families) |

The higher tiers reuse capabilities that already exist elsewhere in the architecture: the
docs adapter's JS-rendered doc-site crawl drives the **L4 Playwright browser** over a site's
sitemap, and vector-indexed retrieval serves doc passages to the agent on demand. Because a
source sits behind a fixed contract, moving it to a higher-fidelity tier is a change inside
one adapter and nothing else (§20).

The **profile** mechanism is the other half of "generic by construction": product-specific
facts (Conductor uses `X-Authorization` raw-JWT, has a token-exchange body, has these
cleanup families) live in `profiles/<product>.json`, never in the engine. Supporting a new
product is a new profile, not new code.

### 11.1 Feature-Exploitation Playbook

Understanding a product's management API is not sufficient. For targets whose primary
capability is itself executable or composable (workflow engines, job runners, pipeline
platforms, integration systems), the harness must **operate the primary feature and use it
as the attack surface**. A target profile may therefore carry a
`feature_exploitation_playbook` containing:

- the documented define → start → poll recipe for the product;
- the feature primitives that can be exercised (task/job/step types);
- the security objective each primitive tests and the evidence required;
- a `must_exercise` set that gates campaign completeness.

The docs adapter supplies the normal operating recipes; the profile sharpens them for a
known product archetype. Profiles may declare canonical `documentation` URLs; selecting the
profile automatically enrolls those sources alongside operator-supplied docs, including
JS-rendered crawl and indexing. The exploit agent executes the resulting recipes through `code_exec`, while the
sandbox records a machine-readable **operation ledger** (definitions, execution IDs, task
types, CVE attempts). Reflection may suggest the next move, but it cannot declare completion:
the deterministic campaign-progress gate requires every applicable playbook primitive and
the top version-matched CVE to be completed or explicitly blocked. A created definition
without a recorded execution never counts as exercising the product.

Operator-pinned objective runs use the same forcing mechanism. A focused catalog objective
is inserted as a deterministic mandatory hypothesis, not merely prompt guidance, and remains
in the completion gate until an exploit-agent execution is recorded or its catalog-declared
capability/identity prerequisite is reported blocked. The operation ledger also records
secret-free direct HTTP actions (method, path, identity, status) so non-workflow product
objects and endpoints cannot disappear from the final evidence trail.

---

## 12. Verification & epistemics

The credibility of the whole system rests here.

- **Separation of powers.** The agent that *exploits* is not the agent that *verifies*.
  The verifier is prompted to *refute*, defaulting to "rejected" under uncertainty.
- **Per-class evidence bars.** Each objective class declares what counts as proof
  (e.g. cross-tenant requires reading *another tenant's* distinctive data, not a 200).
- **Out-of-band confirmation.** Blind/SSRF-class findings are confirmed via an independent
  OOB collaborator callback, not by inference from the response.
- **Provenance & contradiction.** Because every assertion is tagged
  observed/documented/source/inferred, the system flags *unresolved contradictions* (docs
  claim invariant X; observation violates X) as high-value leads rather than silently
  trusting either side.
- **Breadth across passes, not a single-pass completeness claim.** A pass tests a bounded
  set of hypotheses; breadth accrues from reflection-driven multi-pass coverage (§5) and
  cross-run memory (§18), and the coverage ledger states what remains untested rather than
  implying a clean bill.

---

## 13. Cross-tenant & data-exfiltration chain

The first catastrophic outcome (§1): confidential data — another tenant's or user's records,
or classified material (PII / PHI / PCI / secrets) — reaching a principal not entitled to it.
This is the most **relational** of the chains: many leaks exist only *between* parties, so it
is driven by the identity model (§9) and confirmed by *whose* data was read, not by a status
code.

```
   MAP ──▶ REQUEST-AS ──▶ CLASSIFY ──▶ CONFIRM
   locate     re-issue the    bucket what     evidence bar:
   sensitive  request as a    came back:      another party's
   data & its different       pii/phi/pci/    distinctive data,
   owner      principal       secret/infra    quantified — not a
   (objects,  (BOLA/IDOR,     (the DLP        200, not the
   exports,   cross-tenant,   classifier)     caller's own data
   sinks)     enumeration)
```

The chain composes catalog objectives (§6) — `CONF-CROSS-TENANT-READ`, `CONF-BOLA-CROSS-USER`,
`CONF-EXCESSIVE-DATA`, `CONF-SINK-LEAK`, `CONF-ENUMERATION` — into one goal: data crossing an
entitlement boundary.

Design rules:

- **Relational adequacy gates the claim.** Cross-tenant / cross-user requires ≥2 distinct
  parties (§9); without them the cell is *blocked*, never "clean." A leak is proven only by
  reading party B's data while authenticated as party A.
- **The bar is *whose* data, not how much or what status.** A 200 returning the caller's own
  data is nothing; the evidence bar (§12) is another principal's identifiable record,
  *quantified* ("3 other-tenant emails + 1 cloud key") via the sensitive-data classifier,
  which weights a leaked secret far above a common email.
- **Classify, don't just match.** Returned data is bucketed (pii / phi / pci / secret /
  financial / infra) so impact is graded — a PHI or secret leak outranks a support email.
- **Minimal exposure — proof, not pillage.** The harness reads *enough to prove* and stops;
  bulk exfiltration is unnecessary and a volume-budget / halt concern (§10).
- **Sinks count.** Leaks via logs, exports, notifications, reports, caches, and source maps
  are in scope (`CONF-SINK-LEAK`), not just direct object reads.

Composition: identities & personas (§9), the `http_request` hand (§7 L4), the sensitive-data
classifier (§11), the evidence bar (§12), and the scope / volume / halt gates (§10). A
confirmed cross-tenant read often becomes **chaining** input (§5) — e.g. another tenant's id
or token unlocking a deeper pass.

---

## 14. Known-CVE exploitation chain

Publicly-known vulnerabilities in the target's dependencies and stack are a first-class
attack avenue — something the harness **attempts**, not merely reports. This is a worked
vertical that composes the existing layers; there is no special-case engine.

```
   DETECT ──▶ MATCH ──▶ PRIORITIZE ──▶ ATTEMPT ──▶ CONFIRM
   L2 deps      query a       reachable ×    L3 hypothesize    L5 evidence bar:
   adapter:     vuln DB        version-       (driven by the   demonstrated
   source       (OSV) for      matched ×      catalog) runs    impact — never
   manifests →  CVEs on those  severity       the published    version-presence
   resolved     exact          ranking        exploit via L4   alone
   versions;    versions                      code_exec; OOB
   or stack                                   confirms blind
   inferred                                   RCE/SSRF
   from
   fingerprint
```

The chain is **catalog-driven**: an `INFRA-SUPPLY-CHAIN` objective (§6) is applicable
whenever source or a known stack is present, and its `how_to_test` mandates *attempting the
published exploit* (deserialization / SSTI / RCE / auth-bypass) for the most severe
**reachable, version-matched** CVE — so the reasoning layer is pulled toward exploitation,
not a list.

**Where CVEs come from (and why it is queried live).** CVE knowledge is *never* a value
compiled into the engine; it is fetched from authoritative feeds at the start of the run, so
a campaign reasons over today's vulnerabilities, not a stale snapshot. The feeds serve four
distinct purposes:

| Purpose | Source(s) | Access |
|---|---|---|
| **Identity** — does a CVE exist for this package@version? | OSV.dev (multi-ecosystem), NVD (NIST), GitHub Advisory Database (GHSA) | OSV `POST api.osv.dev/v1/query` (free, no key); NVD `services.nvd.nist.gov/rest/json/cves/2.0`; GHSA GraphQL |
| **Severity** — how bad, on what vector? | CVSS (carried in NVD/OSV/GHSA) | in the identity feeds |
| **In-the-wild exploitation** — is it *actually* being exploited? | CISA **KEV** catalog; FIRST **EPSS** (exploit-probability) | KEV JSON feed; EPSS `api.first.org/data/v1/epss` |
| **Weaponization** — does a usable exploit/PoC exist? | Exploit-DB, Metasploit modules, Nuclei templates, public PoC indexes, vendor advisories (Spring, Apache, …) | template/index lookups |

These are **reference data behind an adapter**, not engine logic: adding a feed or a new
ecosystem is a data/adapter change (§20). Prioritization for *which* CVE to attempt weights
**reachable × version-matched × severity × (KEV / high-EPSS) × exploit-available** — a CVE on
the KEV list with a public exploit and a reachable code path is tried before a high-CVSS one
with no known exploit.

**Freshness rule.** The identity feeds (OSV/NVD/GHSA) are queried live per run; the KEV and
EPSS lists are refreshed at run start; the result carries a `feeds_as_of` timestamp into the
report. The harness never uses a bundled CVE snapshot to *confirm* a finding — this is part
of the start-of-loop intel refresh (§5).

Design rules that keep this honest and safe:

- **A version match is a lead, not a finding.** The mere presence of a vulnerable version is
  INFO ("verify"), never a confirmed vulnerability. It becomes a finding only when the
  exploit *demonstrably produces its impact* against the live target, held to the per-class
  evidence bar (§12) — the same adversarial discipline as everything else (§2). Confirming
  by version string would be exactly the absence-of-evidence-as-evidence error §2 forbids.
- **Version-unknown ranks below version-matched.** A CVE whose applicability can't be tied to
  a resolved version is capped at INFO and is never auto-selected to exploit, so unresolved
  dependency versions can't inflate risk or misdirect effort.
- **Reachability scoping.** A CVE in a dependency not reachable from the live attack surface
  is ranked down and reported as *present but not demonstrably reachable*, not exploited
  blindly.
- **Capability-gated.** Running a real exploit is a higher-capability action: RCE or
  state-changing exploitation requires the manifest's capability grant (§10) and is refused
  below the ceiling like any other action.
- **DoS/availability CVEs route to the resilience tier.** A CVE whose impact is
  denial-of-service is never "proven" by flooding; it routes through the availability chain's
  gated, bounded resilience capability (§16) or is reported attempted-not-confirmed.
- **Distinct provenance.** A CVE lead is a *documented* assertion (from the vuln DB); a
  confirmed exploit is an *observed* one — recorded as two records, not conflated (§2).

Because each step is an existing layer — the `deps` adapter (§11), the catalog (§6),
reasoning (§7 L3), `code_exec` + OOB (§7 L4), the evidence bar (§12), the capability gate
(§10) — extending it is localized (§20): a new exploit technique is a prompt change, and a
new dependency ecosystem or vulnerability database is an adapter change.

---

## 15. Infrastructure & secret-extraction chain

The third catastrophic outcome (§1): using the application as a **lever to compromise
its hosting substrate** — the cloud account (AWS / GCP / Azure / OCI), the orchestrator
(Kubernetes), or the host (VM / bare metal). The architectural insight that makes it a
distinct chain: the app *holds an infrastructure identity it never earned for the attacker* —
a cloud IAM role, a service-account token, database credentials, secret-manager access,
metadata-service reachability. The attack is to make the app **surrender or wield that
identity**, crossing the trust boundary from *application-plane* compromise (one tenant) into
*substrate* compromise (the account / cluster / host). The catastrophic prize is **material
an external actor can replay from their own machine** — the literal "used separately to
exploit further."

```
   FINGERPRINT ──▶ REACH ──▶ EXTRACT ──▶ VALIDATE ──▶ CHAIN
   infer the      coerce the    pull infra      prove the      record as PIVOT
   substrate      app to act     secrets/        material is     material:
   (cloud/k8s/    on the infra    config:         live &          extends the
   host) from     plane: SSRF→    IMDS creds,     replayable      attack graph +
   headers, LB    metadata,       SA token,       FROM OUTSIDE    residual risk;
   fingerprint,   internal svc,   env/actuator,   the victim      does NOT roam
   IMDS probe,    file-read,      unmasked        (bounded,       the substrate
   stack          code-exec       integration     read-only)      beyond scope
   inference                      secrets, error
                                  leakage
```

The chain composes three catalog objectives (§6) — `INFRA-SSRF` (reach internal
services / cloud metadata / secret managers), `INFRA-RCE-INJECTION` (server-side code or
template execution), and `INFRA-SECRET-SURFACE` (harvest secrets from exposed surfaces) —
into one goal: substrate material an attacker can carry away.

**Techniques, parameterized by substrate (data, not code):**

- **Metadata-service abuse via SSRF.** When the app exposes an outbound-fetch feature
  (webhook, import-from-URL, HTTP task, PDF/image render), the metadata endpoint is the
  highest-value target: AWS IMDS (including the IMDSv2 token handshake), GCP
  `metadata.google.internal` (+ `Metadata-Flavor` header), Azure IMDS (+ `Metadata: true`),
  OCI `/opc/v2/`. These return **short-lived cloud IAM credentials** — replayable against the
  cloud control plane.
- **Orchestrator / host secrets** where file-read or code-exec is achieved: the Kubernetes
  service-account token and mounted Secrets/ConfigMaps, `/proc/self/environ`, cloud credential
  files (`~/.aws/credentials`, gcloud config, kubeconfig), and internal control-plane
  endpoints (API server, kubelet, etcd) reachable via SSRF from inside the pod.
- **Exposure through the app's own surfaces (no SSRF required):** debug/observability
  endpoints that dump configuration and memory (e.g. a framework actuator's `env` /
  `configprops` / `heapdump`), verbose errors and stack traces leaking connection strings and
  internal hostnames, metrics/topology endpoints, exposed `.git` / `.env` / backups, and
  **integration or connector configurations returned with secrets unmasked** — each a direct
  infra-secret leak.
- **Egress & topology mapping:** which internal CIDRs, service-mesh peers, and cloud-internal
  endpoints the app can reach — reconnaissance of the substrate from inside the app, confirmed
  blind via the OOB collaborator.

**Substrate metadata reference (the targets of the SSRF/EXTRACT step).** The endpoints,
headers, and credential paths per substrate are held as **reference data** so the chain is
generic across providers; the table below is the *shape* of that data, not engine logic.

| Substrate | Metadata endpoint | Required header / step | Credential / key path |
|---|---|---|---|
| **AWS** (IMDSv2) | `169.254.169.254` (IPv6 `[fd00:ec2::254]`) | `PUT /latest/api/token` (`X-aws-ec2-metadata-token-ttl-seconds`) → token in `X-aws-ec2-metadata-token` | `/latest/meta-data/iam/security-credentials/<role>` |
| **AWS** (IMDSv1) | `169.254.169.254` | none | same as above |
| **GCP** | `metadata.google.internal` (`169.254.169.254`) | `Metadata-Flavor: Google` | `/computeMetadata/v1/instance/service-accounts/default/token` |
| **Azure** | `169.254.169.254/metadata/...` | `Metadata: true` | `/metadata/identity/oauth2/token?resource=…` |
| **OCI** | `169.254.169.254/opc/v2/` | `Authorization: Bearer Oracle` | `/opc/v2/instance/`, `/opc/v2/identity/cert.pem` |
| **Alibaba** | `100.100.100.200/latest/meta-data/` | none | `/latest/meta-data/ram/security-credentials/<role>` |
| **DigitalOcean** | `169.254.169.254/metadata/v1/` | none | `/metadata/v1/` |
| **Kubernetes** (in-pod) | API `https://kubernetes.default.svc` (env `KUBERNETES_SERVICE_HOST`) | bearer SA token | file `…/serviceaccount/token`; kubelet `:10250`, etcd `:2379` |

**Where current conventions are sourced.** This pack is refreshed from authoritative vendor
documentation (the AWS / GCP / Azure / OCI instance-metadata references) and corroborated
against community technique indexes (e.g. SSRF / cloud-metadata payload collections, scanner
template sets). New providers, IP changes, or a new IMDS version land as a data update.

**Freshness rule.** The substrate-metadata pack is versioned and refreshed as part of the
start-of-loop intel refresh (§5), and a run records the pack's `as_of` version — so a
campaign never probes a substrate with retired endpoints or misses a newly-introduced one
(e.g. an IMDSv2 handshake) because of a stale bundled list.

**Design rules — honesty, and the scope hazard, which is sharpest here:**

- **The finding is *replayable material*, not a suggestive string.** The evidence bar (§12)
  for infra extraction is *independent replay*: the extracted credential authenticates, or the
  secret grants access, when exercised **from outside the victim** — never "the response
  contained something key-shaped." A masked, expired, or self-issued value is not a finding.
- **Validation is bounded to proof-of-validity.** Confirming a stolen cloud credential is a
  single read-only identity check (e.g. caller-identity / token introspection). The harness
  does **not** enumerate or act across the cloud account — that is a *different target*.
- **The substrate is its own scope.** SSRF *through the app* to the metadata IP is the app
  acting (in-scope of the app); the harness *directly wielding* extracted credentials against
  the cloud control plane is a new target requiring explicit manifest authorization for that
  plane (§10). Absent it, extraction is **confirmed-and-halted, not pursued**.
- **Extraction is a halt-worthy signal.** Pulling real cloud / orchestrator credentials is
  exactly the "unexpected real sensitive data / out-of-scope reach" the halt conditions (§10)
  watch for; the safety governor can stop the campaign at the boundary rather than let it
  wander into the substrate.
- **Substrate knowledge is data.** Metadata endpoints, required headers, and secret paths per
  cloud/orchestrator are catalog/reference data, so AWS/GCP/Azure/OCI/Kubernetes/bare-metal are
  covered generically and a new substrate is a data change (D1/D3).
- **Extracted material is pivot material.** A confirmed infra secret becomes a node in the
  attack graph (§8) and a line in the residual-risk statement (§18) — modeling the follow-on
  blast radius even though the harness itself stops at the boundary.

Composition: fingerprint/stack inference (§11); the SSRF / file-read / code-exec hands
(§7 L4: `http_request`, `code_exec`, `oob`); the sensitive-data classifier that already
buckets infra/secret material — IMDS address, connection strings, cloud keys, tokens (§11,
§12); the evidence bar (§12); and the scope / halt / capability gates (§10). A new cloud or a
new exposure technique is a catalog-data or prompt change (§20).

---

## 16. Availability & denial-of-wallet chain

The second catastrophic outcome (§1): driving the target into instability, resource
exhaustion, or unbounded cost. Uniquely among the chains, the *act of testing is itself
dangerous* — a naive "does it fall over?" test *is* an attack. So availability testing is a
**gated, safe-by-construction capability**, off by default, that probes for the *knee* (the
onset of degradation) and aborts there, rather than proving impact by causing an outage.

```
   IDENTIFY ──▶ RAMP ──▶ READ THE KNEE ──▶ STOP
   find an        bounded,       watch latency /    abort at the
   amplifier:     stepped load   error rate per     first sign of
   unbounded      [1,2,4,8…]     step; the knee     degradation —
   work, ReDoS,   never a flood  is where the       impact is the
   N+1, costly                   curve bends        knee, proven
   op, fail-open                                    WITHOUT an outage
   rate-limit
```

The chain composes catalog objectives (§6) — `AVAIL-COMPLEXITY`, `AVAIL-REDOS`,
`AVAIL-RATELIMIT`, `AVAIL-COST`, `AVAIL-FAILOPEN` — into one goal: demonstrating an
availability or cost cliff without going over it.

Design rules:

- **Off by default; gated.** This capability runs only when the manifest explicitly grants
  the resilience tier (§10); absent the grant, availability objectives are *blocked*, not
  tested.
- **Prove the knee, not the outage.** The finding is the *onset* of degradation under a
  bounded, stepped ramp — never a sustained flood or an actual denial of service. The
  `load_probe` self-aborts at the latency/error knee.
- **No flooding, ever.** The ramp is bounded and stepped; it is reconnaissance of the
  resilience curve, not a DoS — which is precisely why it is a distinct capability tier, not a
  normal action.
- **Denial-of-wallet is a first-class impact.** For metered / AI / cloud-cost paths
  (`AVAIL-COST`), the impact is unbounded *spend*, demonstrated by the cost gradient per step,
  not by running up a real bill.
- **Fail-open is the prize.** A rate-limit or quota that *fails open* under load
  (`AVAIL-FAILOPEN`) is the high-value finding — and the safest to confirm, since it shows at
  the knee.

Composition: the `load_probe` hand and its knee analysis (§7 L4), the resilience capability
gate (§10), the evidence bar (§12), and the budget / halt governor (§10). DoS-class CVEs from
§14 route here rather than being "proven" by exploitation.

---

## 17. Detection & response assessment ("would we notice?")

§1's question has a third clause the rest of the design owes an answer to: *would anyone
notice?* A confirmed finding that is invisible to the defender is worse than one that trips
every alarm. After a confirmed finding, the harness optionally runs a **purple-team
assessment** of the target's own prevention/detection/response — turning the campaign from
red-team ("can it be done?") into purple ("can it be done *undetected*?").

What it assesses (best-effort, target-dependent):

- **Prevention** — was the malicious request *blocked* (WAF / 403 vs 200), and was the block
  *surgical* (the benign control case still works)?
- **Detection** — did the target's own audit/event surface record it (the app's audit log,
  event endpoints — the `DETECT-COVERAGE` objective, §6)?
- **Response** — is the recorded signal *alertable* (severity, attribution), and did anything
  fail *safe* vs fail *open*?

Design rules:

- **"Not assessed" is the honest default.** Without log/SIEM access the harness cannot see the
  defender's side; it reports *not assessed*, never "no detection." Absence of an observed
  alarm is not absence of detection (§2).
- **Opt-in and gated.** Purple assessment runs only when enabled and permitted by the
  manifest's allowed techniques (§10) — it is observation, not additional attack.
- **It grades a finding; it does not create one.** Detection observations attach to an
  existing confirmed finding (a new field); they never up- or down-grade the finding's
  *validity*.
- **Detectability feeds residual risk.** A confirmed, *undetected* finding raises residual
  risk in the dossier (§18) more than a detected one — the worst assurance answer is
  "exploitable AND silent."

Composition: the `http_request` hand (§7 L4) for the prevention probe and the audit/event
read, the catalog's `DETECT-COVERAGE` objective (§6), and the residual-risk statement (§18).

---

## 18. Persistence & assurance (L7)

What turns episodic runs into an assurance program:

- **Memory.** State persists per deployment fingerprint (model, ledger, hypotheses,
  findings, tried-signatures, history). A new run loads prior knowledge: it dedupes already
  tried hypotheses, and on a version change marks prior confirmed findings **stale** for
  revalidation (release-triggered invalidation).
- **Coverage ledger.** Multi-dimensional, never a single "% secure." Cells are
  tested/partial/untested/blocked/N/A; the denominator excludes N/A objectives.
- **Benchmark (quality metrics).** A fixed harness (`bench/`) of seeded-vulnerable and
  known-clean targets, with ground-truth findings, yields measurable **FP rate, FN rate,
  recall, and reproducibility**. It is how the harness's own quality is *measured* — and the
  objective ground truth the self-improvement loop optimizes against (§19, H1).
- **Dossier.** The living deliverable: authorization record, app+trust model, personas,
  invariant catalog, coverage, active/rejected/inconclusive hypotheses, confirmed findings,
  the attack graph, detection observations (§17), and an explicit **residual-risk statement**.
- **Compliance & regression.** OWASP/ASVS roll-up from the coverage ledger; PoC bundles
  that a standalone `retest` can re-issue to verify remediation.

---

## 19. Hill climbing: the self-improving meta-loop (loop engineering Level 4)

The chains and the loop above automate the *work*. The outermost loop automates *improvement
of the apparatus that does the work* — and it is the reason the system keeps working as the
world moves. Security is **adversarial and non-stationary**: new CVEs daily, new frameworks,
new attacker tradecraft, an app that changes under you. A statically-authored harness decays
from the day it ships — its ceiling is whatever its authors knew once. Hill climbing is the
only mechanism here that lets the engine **track a moving target and compound.** It is the moat.

But it is an **amplifier, not a source of truth.** Pointed at a great ground-truth oracle it
compounds toward excellence; pointed at a weak one it compounds toward *confident garbage* —
faster than a human would. Three things therefore stack, in order of foundation:

1. a sound **epistemic floor** (verification, §12) and **safety boundary** (§10) — which HC
   is *forbidden* to touch (H2), so it can never lower the correctness or safety guarantee;
2. a **great oracle** — the living benchmark (§19.2) — which is what HC actually climbs;
3. the **HC engine** (§19.3–19.10), which compounds (2) on top of (1).

**The oracle is the foundation; HC is the engine.** Most of the design below is spent making
the oracle trustworthy, because a self-optimizer is exactly as good as the signal it climbs.

### 19.1 What "hill climbing" means here

In the loop-engineering taxonomy
([LangChain](https://www.langchain.com/blog/the-art-of-loop-engineering)), loops stack in
four levels. **Level 4 is not payload-level optimization** — it is a *meta-loop* that
analyzes the system's own run traces and **rewrites the harness's configuration**, so that
*"the return arrow doesn't just loop back to the top — it reaches inside and updates the
[inner] loop directly. Each cycle of the outer loop makes the inner loops more effective."*
Lower levels automate the *work*; Level 4 automates *improvement of the apparatus*.

| Level | Loop | Where it sits in this architecture |
|---|---|---|
| Level 1 | **Agent loop** — call tools until done | `exploit_agent` / `explore_agent` (§5) |
| Level 2 | **Verification loop** — score vs a rubric, retry on fail | `verify_finding` + per-class evidence bars (§12) |
| Level 3 | **Event-driven loop** — external events invoke a run | a CI job / release webhook calls `assess(I)` (§3) |
| Level 4 | **Hill climbing loop** — traces → analysis agent → rewrite config | this section |

The hill-climbing loop is an **analysis agent** that reads the accumulated trace corpus and
rewrites the harness's *own configuration* — its reasoning prompts, its objective catalog,
its target profiles, its evidence-bar calibration — which subsequent runs then use. It is
the loop that makes the *generic engine* get better at being generic, run over run.

### 19.2 The oracle: a living, held-out, adversarial benchmark

HC reads the high-signal **trace corpus** every run produces — per-deployment memory (§18:
confirmed/rejected/inconclusive findings, tried signatures, coverage, history), coverage
ledgers, and verifier verdicts *with reasons*. But traces are only the *symptoms*; the
**fitness signal is the benchmark** (§18), and the benchmark's quality bounds everything HC
can become. Three disciplines turn it from an overfitting trap into a trustworthy oracle:

- **Living.** Every human-ratified *real-world* finding is distilled into a permanent
  benchmark fixture. The oracle grows from production, so HC optimizes against an ever-richer
  truth — and the same corpus is a **forgetting guard**: a past win must keep passing forever
  (a change that re-breaks it is rejected).
- **Held-out.** The proposer never sees the fixtures used to *promote*. A champion is promoted
  only on improvement over a **held-out** split (K-fold across targets) the optimizer cannot
  train on — the direct antidote to the #1 failure mode of any self-optimizer: climbing the
  benchmark hill while the real-world hill is elsewhere.
- **Adversarial.** The corpus deliberately pairs **near-miss negatives** ("looks like a vuln,
  isn't") with **subtle positives** ("a real vuln that barely shows"), so precision *and*
  recall are both under tension. A benchmark of only easy positives teaches HC to be
  trigger-happy.

The oracle reports its own **coverage** — the fraction of catalog objectives with at least one
fixture. On a class with **no fixture, HC is "unmeasured" and must not auto-tune**: honesty
about where the oracle is blind, rather than optimizing in the dark.

The analysis agent mines the traces, forms an improvement hypothesis ("the verifier rejects
cross-tenant findings for reason X, but the oracle says those were real"), and routes it
through the gates of §19.4–19.5.

### 19.3 Design constraints — self-improvement in a security context

A loop that rewrites the apparatus is powerful and, in a security context, must be
tightly constrained. Each constraint below is paired with the design rule that enforces it.

- **H1 — The fitness signal is ground truth, never self-report.** A loop that optimizes
  against the harness's *own* verdicts learns to *report* success, not to *be* correct — it
  tunes itself to confirm more, manufacturing false positives. **Rule:** the fitness
  function is the **benchmark** (objective FP/FN/recall on seeded + clean targets) plus
  human-ratified outcomes — not the harness's unverified confirmations.
- **H2 — The truth machinery and the safety boundary are off the optimization surface.** A
  loop that "improves recall" by loosening an evidence bar, or "improves yield" by relaxing
  scope/capability, has defeated the two things the whole design exists to guarantee (§2:
  *adversarial*, *safe*). **Rule:** evidence bars / graders and the entire safety/authz layer
  (manifest, capability levels, scope, halt, guardrail — §10) are **never auto-tuned**; the
  loop may *propose* evidence-bar recalibration but only a human ratifies it, and it may
  never touch authority.
- **H3 — The generic engine is not overfit to one target (the local optimum).** Climbing
  against a single target pulls the *core* prompts/catalog toward that target — the local
  optimum that violates "generic by construction" (§2). **Rule:** target-specific learnings
  land in the **profile** (§11), not the core; a change to core prompts/catalog must not
  regress the benchmark across *multiple* targets before it is kept.
- **H4 — Changes act on corroborated signal, not single-run noise.** LLM nondeterminism
  means one trace's "failure" may be sampling noise. **Rule:** a problem must recur across
  *multiple* traces before a change is proposed ("when multiple traces signal a potential
  problem").
- **H5 — Every config change is a provenanced, reversible assertion.** **Rule:** each change
  is versioned, attributed to the traces that motivated it (provenance, §2), benchmark-scored
  before/after, and rolled back automatically on regression. The catalog, prompts, and
  profiles are all versioned.
- **H6 — The loop proposes; a human ratifies the consequential.** Self-modification of a
  system that makes security claims is not fully autonomous. **Rule:** a ratification gate
  sits between proposal and adoption for any truth- or safety-adjacent surface.
- **H7 — Traces are untrusted input to the optimizer.** A target can emit responses *crafted
  to appear in traces and steer HC* (e.g. to coax it into loosening a check). **Rule:** the
  guardrail (§10) extends to HC — trace content is untrusted *data*, never instruction; a
  target may influence traces but never the benchmark or the promotion gate, both of which are
  under the operator's control.

### 19.4 The optimization surface (the central design artifact)

Hill climbing is, concretely, a policy over **what it may change and how**:

| Surface | Mode | Rationale |
|---|---|---|
| Target **profiles** (auth scheme, cleanup families, quirks) | **auto-tune** (benchmark-gated) | target-specific, isolated from the core; safe to learn |
| Reasoning **prompts** (`hypothesize`, `exploit`, `reflect`) | **auto-propose → benchmark-gate → keep if no multi-target regression** | improves method; must not overfit |
| **Catalog** content (`how_to_test`, new objective candidates) | **propose → human-ratify** | expands what "catastrophic" means — needs judgment |
| **Evidence bars / graders** | **propose → human-ratify only** | this *is* the truth machinery (H2) |
| **Safety / authz** (manifest schema, capability rules, scope, guardrail) | **never tunable** | optimizing this defeats the design (H2) |

### 19.5 The improvement cycle — sound, attributable, hack-resistant acceptance

"Climbing" is search over the configuration space toward a higher **fitness** score, under
one iron discipline: *it never steps downhill on the metrics that matter*. One step is an
**epoch** over a batch of accumulated traces (never a single run — H4):

1. **Observe** — cluster recurring failure signatures across runs and targets.
2. **Diagnose** — root-cause each cluster to a *specific* configuration surface (map below):
   not "the harness underperformed" but "objective X's `how_to_test` doesn't elicit the
   technique the oracle's seeded vuln requires."
3. **Propose minimal, attributable challengers** — each challenger is the *smallest single-
   surface diff* that addresses one diagnosis, so its benchmark delta is causally
   attributable. Bundling several edits into one accept is forbidden — it teaches the
   optimizer superstitions (which edit actually helped?).
4. **Evaluate with statistical rigor** — score each challenger over **N seeded, paired runs**
   on the **held-out** targets (§19.2), recording a confidence interval, not a point estimate.
5. **Accept only beyond the noise floor** — a challenger replaces the **champion** only if its
   held-out improvement is **statistically significant** (clears the CI band / exceeds a
   minimum detectable effect) **and** regresses **no protected metric and no per-class
   recall**. "Strictly beats" means *beats beyond noise*, not *won one lucky run*.
6. **Commit** — the new champion is signed, content-addressed, and `as_of`-stamped with the
   pinned (model, benchmark, seed) and the justifying traces; the prior is retained for
   rollback (H5); the next campaign's inner loops load it (§5).

**Fitness is a constrained, hack-resistant objective.** It maximizes ↑ **recall**, ↑
**coverage**, and ↑ **attempt-rate on applicable objectives** — *subject to the hard
constraints* that **false-positive rate must not rise**, **no per-class recall may drop**, and
**cost/latency must not materially worsen**, with **reproducibility** as the tie-breaker.
Three of these close specific reward-hacking holes:

- the **FP constraint** (per §2, the expensive failure) makes "buy recall by loosening
  precision" structurally impossible — which is *why* evidence bars are human-ratify-only (H2);
- **attempt-rate** stops HC from "winning" by quietly *avoiding* hard classes (fewer attempts
  → fewer visible misses);
- **per-class non-regression** stops an aggregate gain from silently killing a rare class.

Where ground truth can be **deterministic** (a known seeded vuln) the oracle uses it rather
than an LLM grader, so the proposer cannot game the grader. The non-regression rule makes the
champion's held-out fitness *monotone*: the engine can only get better, never quietly worse.

**Diagnosis → surface map** (which symptom turns which knob, drawn from 19.4):

| Recurring trace signature | Diagnosis | Candidate change | Surface (mode) |
|---|---|---|---|
| Benchmark FN — a seeded-vuln class is missed | hypothesis breadth / exploit technique weak for the class | enrich the class prompt / `how_to_test` | prompt (auto) · catalog (ratify) |
| Benchmark FP — a clean-app finding is confirmed | evidence bar too permissive for the class | tighten the evidence bar | evidence bar (ratify only) |
| Many hypotheses, zero confirmations on an objective | objective guidance vague / not actionable | sharpen `how_to_test` | catalog (ratify) |
| Repeated auth failures / 401s on a target | profile auth scheme or token-exchange wrong | correct the profile | profile (auto) |
| Adapter low yield (e.g. docs nav-shell only) | wrong fidelity tier selected for the source | adjust adapter-tier config | profile/adapter (auto) |
| High cost or stalls per confirmed finding | redundant probing / poor ordering | prompt / ordering tweak | prompt (auto) |
| A coverage cell is persistently **blocked** | missing identity or capability — an *input* gap | **surface to the operator; do not tune around it** | none (reported) |

The last row is the honesty guard: not every failure is the harness's to fix. A cell blocked
for lack of a second tenant is a missing *input* (§9), not a configuration defect — the loop
reports it and requests the identity rather than "optimizing" coverage by lowering the bar.

### 19.6 Search strategy — beyond greedy

Greedy single-champion search plateaus and overfits. The optimizer is therefore:

- **Population-based, not single-champion.** HC maintains a small *population* of configs and
  a **Pareto frontier** across the objectives (e.g. a fast-cheap champion and a
  thorough-expensive one); the manifest/operator picks the operating point. A frontier resists
  local optima far better than one hill-climber and preserves genuine trade-offs instead of
  collapsing them to a scalar.
- **Budget-aware exploration.** Held-out evaluation is the cost bottleneck, so eval budget is
  allocated by **successive halving / bandit** — obviously-bad challengers die cheap, freeing
  budget to explore widely.
- **Bounded annealing.** Exploratory *downhill* moves are permitted **only on the unprotected
  dimensions** (recall / coverage / cost) and **never on FP or per-class recall** — escape
  from local optima without ever risking the precision floor.
- **Multi-target by default** (H3): a change that helps one target but not the held-out others
  is a local optimum in target-space and is rejected. Convergence is "no challenger beats the
  champion," not a fixed iteration count; on a plateau the search for a surface **pauses until
  new trace signal** (new targets/runs) reopens it.

### 19.7 Optimizing the text surfaces

The profile/threshold surfaces are numeric and yield to standard optimization; the
high-value surfaces — **prompts and catalog `how_to_test`** — are *natural language*, the
harder problem. The design treats them with real prompt-optimization method, not vibes:

- **Concrete exemplars as the gradient.** The proposer is handed the *actual failing trace* —
  the exact missed seeded vuln and its transcript — as the signal, not an aggregate "recall is
  low." Counterexamples produce far better edits than statistics.
- **Method/tactics decomposition.** Each prompt is split into a **frozen method core** (how to
  think for this phase) and a **tunable tactics/exemplars block** — the *only* editable unit.
  This bounds blast radius and sharpens credit assignment.
- **Established optimizers** (OPRO / DSPy / TextGrad-style score-guided textual search) drive
  candidate generation; the held-out oracle (§19.2) is their objective.

### 19.8 Hardening the optimizer, and discovering the unknown

The optimizer is a *self-modifying, security-critical* component, so it is built to be
adversarially safe, reproducible, and able to grow the catalog — not just refine it:

- **Reproducible lineage.** Every champion is a signed, content-addressed artifact carrying
  the pinned (model, benchmark, seed) and the justifying traces, so one can reproduce *exactly
  why* vN replaced vN-1. A **base-model change is a distribution shift** that triggers an
  automatic **re-baseline** — a champion learned under an old model is not trusted under a new
  one.
- **Shadow promotion.** A promotion-pending challenger first runs in **shadow** on real
  campaigns — its findings flagged *experimental* and never acted on — to gather live evidence
  beyond the benchmark before it goes live.
- **Graded autonomy.** The ratification gate (H6) is graded by *blast-radius × held-out
  confidence*: small, high-confidence changes auto-adopt with post-hoc review; high-blast or
  low-confidence ones block on a human handed the failing→passing exemplars, the held-out
  delta with CIs, and the diff — so ratification stays high-signal, never a rubber-stamp.
- **A novelty channel for unknown-unknowns.** HC refines *known* catalog classes; it cannot,
  by itself, find a class the catalog lacks. So the analysis agent runs a discovery pass:
  clusters of confirmed findings that map to **no existing objective** become *proposed new
  catalog classes* (with a candidate `how_to_test` and a new oracle fixture) routed to human
  ratification. This is how HC contributes to *discovery*, not only exploitation.

### 19.9 Cold-start and cross-deployment transfer

- **Cold-start.** With no traces yet, HC bootstraps by running the benchmark itself to
  generate initial traces, on a **curriculum** from easy to hard fixtures.
- **Transfer is the flywheel.** A champion learned across *many* deployments is a
  *generic-engine* improvement that lifts **every** target at once — the new cloud probed
  better for one customer sharpens the infra chain for all. Per-target quirks stay in profiles
  (H3); generic gains propagate. This cross-deployment compounding is the strongest form of
  "knowledge compounds" (§2) and the real long-run moat.

### 19.10 Shape of the loop

```
   ┌──────────────────────────────── next epoch ◀───────────────────────────────┐
   │                                                                             │
 runs ─▶ TRACE CORPUS (memory · coverage · verdicts)  +  LIVING ORACLE [§19.2]   │
              │  cluster failures (multi-trace H4; traces untrusted H7)          │
              ▼                                                                  │
       ANALYSIS AGENT ─ diagnose ─▶ minimal, attributable CHALLENGERS (+ prov.)  │
              │            (population / Pareto frontier — §19.6)                 │
              ▼                                                                  │
       HELD-OUT ORACLE, N seeded paired runs:   ↑recall ↑coverage ↑attempt       │
              │   s.t.  FP not↑ · per-class recall not↓ · cost not↑              │
              ▼                                                                  │
       improvement statistically significant (beats the CI band)?               │
              ├─ no  ─▶ discard ────────────────────────────────────────────────┤
              └─ yes ─▶ profile / prompt   : SHADOW ─▶ adopt (signed, pinned)    │
                        catalog / evidence : HUMAN RATIFY ─▶ adopt               │
                            │                                                    │
                            ▼  new champion (or Pareto point)                    │
                  reaches inside the next run's inner loops (§5) ────────────────┘
```

The cycle is monotone on the protected metrics by construction: a step is taken only when a
challenger *climbs* on the **held-out** oracle beyond the noise floor, without regressing FP
or any per-class recall — so the live configuration only ever improves, and never on a lucky
run. This realizes **knowledge compounds** (§2) at the level of the *engine itself*, not just
the findings — while H1–H7 keep that self-improvement from eroding the honesty and safety (§2)
the rest of the architecture guarantees. It composes into the system the same way every other
capability does: as a localized, gated extension (§20), gated unusually tightly here because
its blast radius is the harness's own credibility.

---

## 20. Extension model — how the system grows

The architecture is judged by this property: *common evolutions are localized changes, not
rewrites.* Each row below is the complete footprint of that change.

| To add… | You touch | You do **not** touch |
|---|---|---|
| **A vulnerability class** | a catalog entry (objective) + its evidence bar in `verify` + a coverage hook + a benchmark fixture | the loop, the workflows, other classes |
| **A knowledge source / fidelity tier** | one L2 adapter behind its fixed contract | the catalog, reasoning, action, safety |
| **A new action ("hand")** | a worker in L4 + its taskdef + add to the fleet | the loop structure |
| **A new target product** | a profile in `profiles/` (auth scheme, cleanup families, quirks) | the engine |
| **A new substrate (cloud/orchestrator)** | a row of metadata reference data (§15) | the infra chain logic |
| **A capability tier** (e.g. resilience) | a manifest-gated capability + a bounded worker + an authz check | the default safe path |
| **A reasoning improvement** | a prompt in `prompts/` | code |

A proposed change that does *not* fit one of these rows is touching an architectural seam:
it deserves a design decision recorded here, not an ad-hoc patch.

---

## 21. Key design decisions (rationale, in brief)

| # | Decision | Why | Alternative rejected |
|---|---|---|---|
| D1 | Catalog as data spine (§6) | one entry drives scope+hypotheses+coverage+compliance+benchmark; classes are pluggable | hard-coded checks per class |
| D2 | Conductor workflow, single entry point (§3) | deterministic control flow + provable termination + fan-out | a bespoke Python driver loop |
| D3 | Generic engine, target facts as profiles/models (§2, §11) | the harness must test anything, including the platform it runs on | a Conductor-specific tester |
| D4 | Adversarial verifier separate from exploiter (§12) | false positives are the expensive failure; refutation discipline | self-verification by the exploit agent |
| D5 | Manifest + capability levels, fail-closed (§10) | safety must be machine-enforced, not a promise | an `--authorized` boolean |
| D6 | Cross-run memory keyed by fingerprint (§18) | assurance compounds; a run builds on prior knowledge instead of starting cold | stateless single runs |
| D7 | Provenance on every assertion (§2, §12) | enables contradiction detection and honest trust | untagged claims |
| D8 | Adapter layer with explicit fidelity tiers (§11) | knowledge sources vary; fidelity is a first-class fact, upgradable in place | assume every source is high-fidelity |
| D9 | Self-improvement gated by ground truth (§19) | the engine compounds, but never tunes its own truth or safety machinery | unconstrained self-modification |
| D10 | A known-CVE version match is a lead, not a finding (§14) | confirmation requires demonstrated impact; version-presence alone would be evidence-of-absence inverted | reporting CVEs by dependency version |
| D11 | The substrate is its own scope; extraction confirms-and-halts (§15) | wielding stolen infra credentials is a *different target*; validity is proven by bounded replay, not by roaming the cloud account | treating SSRF-reached cloud APIs as in-scope |
| D12 | Time-sensitive intel is refreshed at loop start, `as_of`-stamped (§5, §14, §15) | vuln feeds and substrate conventions change constantly; a stale snapshot silently under- or mis-tests | compiling CVE/metadata knowledge into the engine |
| D13 | Cross-tenant leak is proven by *whose* data, not status or volume (§13) | a 200 or a big response proves nothing; another principal's distinctive data does | counting any returned data as a leak |
| D14 | Availability is a gated tier that proves the *knee*, never the outage (§16) | the test is itself an attack; the safe, sufficient evidence is the onset of degradation | flooding to demonstrate a DoS |
| D15 | Detection is "not assessed" by default, never "no detection" (§17) | the defender's side is usually invisible; silence is not evidence of safety | inferring "undetected" from no observed alarm |
| D16 | The self-improvement oracle is living, held-out, and adversarial (§19.2) | a self-optimizer is only as good as its signal; without a held-out split it overfits the benchmark and decays the engine | a fixed, self-authored benchmark |
| D17 | Promotion requires statistical significance + per-class non-regression (§19.5) | LLM noise makes a single "win" a lucky run; aggregate gains can hide rare-class forgetting | promote on a single benchmark improvement |
| D18 | Traces are untrusted input to the optimizer; only operator-held signal gates promotion (§19.3 H7) | a target can poison traces to steer self-modification; the benchmark and gate must stay outside its influence | trusting trace content to drive config changes |
| D19 | Product-feature exploitation is profile-encoded, executed, and machine-measured (§11.1) | management APIs describe the product but do not exercise its engine; definitions without executions prove nothing | relying on prompt text or hypothesis count to imply product operation |

---

## 22. How the pieces realize the loop (one walk-through)

A single `deep_assess` run, mapped to the layers, to make the composition concrete:

1. `normalize_target` validates the **manifest** (L6), computes the **fingerprint**, and
   loads **prior state** (L7); the **intel refresh** (§5) pulls current vuln feeds and
   substrate-metadata conventions.
2. `surface` + `docs_ingest` + source/`sast` + `dep_cve_scan` build the **app model** and
   **CVE leads** (L2); `catalog_context` computes the applicable **objectives** (the §6
   catalog spine); `personas` are derived (§9).
3. The **pass loop** begins; the **safety governor** gates each pass (L6).
4. `hypothesize` (L3) proposes objective- and persona-tagged hypotheses over the catalog +
   CVE leads — spanning the data-exfiltration (§13), CVE (§14), infra (§15), and availability
   (§16) chains as applicable — skipping tried signatures from memory.
5. `exploit_agent` (L3+L4) attempts each via the capability-gated hands.
6. `verify_finding` (L5) adversarially refutes; survivors become **findings** with evidence
   + provenance; if enabled, a **purple** check (§17) attaches detection observations.
7. `reflect_pass` (L3) reads the **coverage ledger** (L7) and steers the next pass to
   untested/blocked cells *and* to chains a confirmed finding unlocks (§5), extending the
   attack graph.
8. On exit: `triage` + `dossier` + `report` produce the assurance deliverable; `memory_save`
   (L7) merges findings, updates coverage, and records the run — so the next run compounds and
   the trace corpus feeds the self-improvement loop (§19).

---

*Companion documents:* requirements → `SECURITY_HARNESS_SPEC.md`; orchestration termination
proof → `docs/EXECUTION_MODEL.md`; spec conformance → `CONFORMANCE.md`; backlog → `docs/ROADMAP.md`.
