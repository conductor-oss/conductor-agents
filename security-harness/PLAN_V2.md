# PLAN V2: Evidence-First, Feature-Led Security Harness

## Goal

Turn the deep-assessment harness into a trustworthy active security-testing system that:

- learns how an application is actually used through documentation, source, UI, and APIs;
- explores both core and neglected features with isolated attacker personas;
- derives exploit attempts from observed behavior and source-to-sink paths;
- confirms impact with deterministic evidence rather than model assertions; and
- leaves a verifiable cleanup or retention record.

The result must distinguish confirmed live vulnerabilities, deployment-attested source
candidates, and untested or blocked risk. It must never treat request volume, workflow
count, or an LLM conclusion as proof.

## Delivery Order

### 1. Secure the harness trust plane

1. Replace credential values in workflow inputs with opaque credential references.
   - Extend `--id` metadata with `tenant` and explicit `role:user|admin|service`.
   - Resolve tokens, passwords, headers, and browser sessions only in a trusted credential
     broker immediately before a network action.
   - Pass labels, role, tenant, and opaque session handles through Conductor; never pass
     credential values, Playwright storage state, or cookies.
   - Support a local runtime registry for local worker fleets and server-side secret
     references for Orkes deployments.

2. Redact and minimize untrusted data at every boundary.
   - Treat source, documentation, DOM text, API bodies, OpenAPI descriptions, and model
     output as untrusted content.
   - Put retrieved content in typed untrusted-data envelopes; do not let embedded text
     modify agent policy.
   - Store bounded excerpts, hashes, schemas, and classified/redacted summaries only.
   - Add marker-secret regression tests proving that secrets cannot reach workflow history,
     task output, LLM messages, reports, dossiers, SARIF, or logs.

3. Strengthen execution isolation and safety policy.
   - Default to TLS verification; an explicit insecure mode is recorded and bars transport
     assurance claims.
   - Preserve the egress jail, then add destination pinning, redirect validation, DNS
     rebinding protection, and explicit private/link-local policy.
   - Run generated code without host mounts, Docker socket access, ambient credentials,
     privilege escalation, writable root filesystem, or unlimited CPU/memory/process/disk.
   - Enforce manifest request, concurrency, payload-size, and endpoint budgets; require a
     just-in-time capability-3 approval for sensitive proofs.

## 2. Build a complete feature graph

Create a persisted, exhaustive `feature_graph` separate from the concise LLM application
summary. Each feature records:

- capability and business purpose;
- UI routes, API/GraphQL/WebSocket entry points, docs recipes, source handlers, workers,
  schedulers, CLI/admin tools, and dependency use-sites;
- inputs, object IDs, owner/tenant fields, role requirements, state transitions, side
  effects, integrations, auth middleware, and dangerous sinks;
- docs/source/browser/runtime citations and confidence;
- lifecycle state: `discovered`, `reachable`, `baseline_exercised`,
  `adversarially_probed`, `verified_secure`, `verified_vulnerable`, `blocked`, or
  `untested`.

Merge five evidence streams: documentation, browser/UI behavior, observed network traffic,
runtime API behavior, and source/dependency analysis. Do not truncate the machine inventory;
only summarize it before sending it to an LLM.

### Tail-feature classification

Classify every feature into one or more buckets:

- core visible flows;
- low-traffic or low-priority features;
- legacy, deprecated, versioned, compatibility, and fallback APIs;
- source-only, UI-hidden, undocumented, and feature-flagged paths;
- role-specific, admin, maintenance, debug, and error handlers;
- imports, exports, uploads, previews, templates, reports, filenames, locale/timezone, and
  parser paths;
- integrations, callbacks, webhooks, connectors, background jobs, schedules, retries, and
  cancellation;
- alternate interfaces for the same capability: UI, REST, GraphQL, SDK, batch, worker, or
  internal endpoint.

Compute a tail-risk score that favors low-visibility capabilities with sparse tests,
inconsistent/missing auth middleware, complex parsing or serialization, privileged side
effects, legacy code, config gates, and docs/source/runtime contradictions.

## 3. Make active navigation and normal use first-class

Replace shallow browse/request exploration with a stateful `journey_explorer`.

1. Use one isolated browser context per persona and opaque session reference.
2. Support typed, policy-gated actions: navigate, click, fill, select, submit,
   upload synthetic data, download metadata, wait, open modal, switch frame, observe DOM,
   observe network, and screenshot hash.
3. Permit selectors only when observed in the current DOM snapshot; reject hallucinated
   selectors, out-of-scope origins, hidden destructive controls, and unapproved file paths.
4. Capture redacted UI/API request templates, schemas, CSRF behavior, pagination, ownership
   fields, state changes, and error contracts from legitimate journeys.
5. Record a `journey_state` for each synthetic artifact: actor, tenant, predecessor step,
   resulting state, permitted next operations, evidence receipts, and trusted cleanup adapter.

For every high-value feature, execute the intended happy path before abuse. A normal journey
creates or configures only synthetic artifacts, verifies the expected behavior through every
available interface, and supplies the baseline for a later mutation.

## 4. Compile feature-specific exploit campaigns

Add deterministic attack-template generation, with the LLM limited to ranking/refining bounded
candidates. Every plan includes the feature-graph edge, actor/victim personas, preconditions,
baseline, one controlled mutation, safe payload, confirmation oracle, cleanup, and a next-best
branch if blocked.

Map feature behavior to exploit families:

- ownership and tenancy -> BOLA, cross-write, stale access, enumeration, and alternate-interface authz;
- role-gated behavior -> vertical authz, mass assignment, hidden endpoint, and negative-space checks;
- multi-step state -> replay, reordering, races, idempotency, retries, rollback, pause/resume, and timeout abuse;
- URI/integration/webhook/callback -> SSRF, signature bypass, confused deputy, and secret exposure;
- scripts, expressions, templates, queries, archives, documents, and serializers -> injection,
  RCE, traversal, parser, and deserialization ladders;
- browser features -> XSS, CSRF, redirect, clickjacking, and client-side authorization;
- dependency use-sites -> CVE-specific proof only through a reachable feature and impact oracle.

Apply boundary-value and metamorphic probes to tail features: null/missing/duplicate fields,
alternate encodings, Unicode normalization, type mismatches, nested values, size boundaries,
ordering, locale/timezone, lifecycle transitions, replay, and bounded concurrency.

## 5. Make hill climbing evidence-driven

Schedule actions over this space:

`feature x persona x object x state transition x interface x trust boundary x exploit family`.

- Never schedule solely by highest score. Per pass, reserve
  `max(2, ceil(35% of exploration slots))` for untested tail buckets and
  `max(1, ceil(25% of exploit slots))` for tail features that have a baseline or
  source-backed sink hypothesis.
- Select at least one candidate from every non-empty untested tail bucket before repeating an
  already-covered core bucket, except for a verified critical chain or safety halt.
- Reward only new evidence: baseline completion, new graph edge, source/runtime correlation,
  cross-persona differential, impact oracle, or a concrete blocker.
- After equivalent attempts provide no new evidence, pivot to another state, interface,
  identity, encoding, source sink, or chained precondition. Do not reward raw request or
  workflow-execution volume.
- Keep failed attempts as structured negative evidence; they narrow future campaigns rather
  than disappearing into agent prose.

## 6. Evidence, provenance, cleanup, and report gates

1. Add `--deployment-attestation <json>` containing source revision, artifact/build digest,
   target release/version, issuer, and timestamp.
   - Findings are `source_only`, `source_attested`, `runtime_reproduced`, or
     `runtime_oob_confirmed`.
   - Only runtime-confirmed findings affect the live risk rating; source candidates retain a
     separate source-candidate risk rating.

2. Replace free-form agent claims with deterministic, tamper-evident receipts.
   - Receipts bind objective, feature edge, actor/victim, tenant, synthetic artifact, request/
     response fingerprint, workflow definition/execution/task state, OOB correlation, and
     cleanup reference.
   - Agents can propose observations and findings but cannot mark an objective exercised or a
     vulnerability confirmed.
   - Require class-specific proof: cross-persona contrast for BOLA; deployed version + use-site
     + impact oracle for CVEs; signed per-attempt OOB or in-band proof for blind SSRF/RCE.

3. Restrict writes and cleanup to verified synthetic artifacts.
   - `sc.created()` records a verified creation receipt, not an arbitrary delete URL.
   - Derive delete paths from trusted target adapters and the recorded artifact identity.
   - Finalize with deletion, prefix sweep, and independent list/GET absence checks.
   - Emit one final status: `CLEAN`, `RETAINED`, or `UNRESOLVED`.
   - `--leave-evidence` stays explicit and requires owner, expiry, trusted deletion route, and
     manual removal command.

4. Add a shared report-quality gate before PDF generation.
   - Persist the same sanitized Markdown that produced the PDF.
   - Redact secret-like source literals by schema/classification, not regex alone.
   - Validate required evidence-state, identity, docs, tail-coverage, and cleanup sections.
   - Validate Markdown table syntax and generated PDF text/layout; reject literal table pipes,
     missing caveats, and secret leakage.

## 7. Workflow and task changes

Extend the existing Python worker fleet with these tasks:

- `build_feature_graph`
- `classify_tail_features`
- `journey_explorer`
- `run_baseline_journey`
- `schedule_feature_campaign`
- `compile_feature_mutations`
- `verify_exploit_receipt`
- `finalize_cleanup`
- `report_quality_gate`

Wire graph, journey, receipt, provenance, and final-cleanup outputs through `deep_assess`,
`assess_pass`, `explore_agent`, and reporting workflows. Register every new SIMPLE task
definition first, with non-zero poll/response/execution timeouts, retries, and idempotency
requirements. Add bounded workflow-level timeouts for deep, pass, and exploration workflows.

## 8. Test and acceptance plan

Add unit, workflow, browser, and end-to-end fixtures for:

- credential/session non-leakage and persona isolation;
- source-only, hidden, deprecated, feature-flagged, admin, error, worker, and scheduler paths;
- UI-only operations, modal/iframe workflows, uploads, GraphQL, observed XHR discovery, and
  cross-interface inconsistencies;
- BOLA read/write, stale authorization, CSRF, stored/reflected XSS, SSRF, script execution,
  traversal, parser/import bugs, retry/rollback bugs, and reachable/unreachable CVEs;
- synthetic-artifact ownership refusal, clean/retained/unresolved cleanup, OOB replay/spoof
  rejection, scheduler tail quotas, and no-evidence stagnation pivots;
- report/PDF snapshots rejecting raw secrets, malformed tables, unverified source claims labeled
  live, missing blocked-objective explanations, and incomplete tail coverage.

### Definition of done

A new deep run can show, for both obvious and neglected features: how the harness discovered the
capability; how a real user normally exercised it; what source/docs/runtime evidence described
it; which feature-specific exploit mutations were attempted; what oracle proved or refuted
impact; and whether all synthetic artifacts were cleaned or intentionally retained.
