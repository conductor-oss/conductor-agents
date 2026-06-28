# Spec conformance: `SECURITY_HARNESS_SPEC.md`

`SECURITY_HARNESS_SPEC.md` (v2.0) is an implementation-independent operating model for a
*persistent, strategic-grade* adversarial security harness. `security-conductor` is one
concrete realization (Conductor workflows + Python workers + Anthropic LLM tasks). This
table tracks how much of each section is actually implemented, so readers don't assume
the shipped tool is the full spec.

Legend: ✅ implemented · 🟡 partial · ❌ not implemented.

| § | Spec area | Status | Where / notes |
|---|---|---|---|
| 3 | Core research loop | ✅ | `deep_assess` = authorize → learn → model → hypothesize → experiment → verify → chain → cleanup → re-plan, multi-pass (`assess_pass`, `reflect_pass`). |
| 4 | Epistemic model (provenance/confidence) | 🟡 | Findings carry `provenance`/`timestamp`/`fingerprint`/`confidence` + content hash in the persistent store (`common/memory.py`); contradiction assertions emitted. Not yet a full assertion ledger across all layers. |
| 5 | Staged knowledge acquisition | ✅ | Black-box (recon/crawl) → docs-assisted (`docs_digest`) → source-assisted (SAST/route_extract) → reconciliation in `app_model`/`hypothesize`. |
| 6 | Application knowledge model | 🟡 | Rich `app_model` JSON; persisted + versioned per fingerprint, not yet a queryable graph. |
| 7 | Normal-behavior learning | 🟡 | `explore.md` learns behavior; no explicit per-persona pre-mutation baseline. |
| 8 | Threat model / personas | ✅ | Personas are first-class (`common/personas.py`): identity → persona with objectives/success-conditions/constraints, fed to hypothesize/exploit/explore. |
| 9 | Security invariants | ✅ | `documented_invariants` from docs are explicit falsification targets (`docs_digest` → `hypothesize`). |
| 10 | Hypothesis-driven experiments | ✅ | Falsifiable hypotheses (test_plan/expected_evidence/controls); `exploit.md` executes them. |
| 11 | Adversarial methodologies | ✅ | Differential (cross-identity), state/workflow, concurrency/replay (`burst`), parser, source-to-runtime, business-abuse, negative-space, chaining in the prompts. |
| 12 | Campaign planning | 🟡 | Explore/exploit/verify/reflect alternation + loop-until-dry; coverage-gap-driven `focus_directive`. No explicit priority-formula scheduler. |
| 13 | Persistent memory | ✅ | `common/memory.py` durable store keyed by deployment fingerprint: cross-run dedupe (`tried_signatures`), confirmed/rejected ledger, release-triggered invalidation, run history. |
| 14 | Logical role separation | ✅ | Observer/modeler/hypothesizer/executor/verifier/reflector as separate prompts+workflows; independent **safety governor** (`workers/safety`) the planner/executor cannot override. |
| 15 | Authorization model | ✅ | Machine-enforceable manifest (`common/authz.py`): approvers/scope/window/capability/forbidden-ops; capability levels 0–4 enforced at the `http_request`/`code_exec` chokepoints; automatic halt conditions (`common/halt.py`) + kill-switch + window-expiry via the per-pass `safety_check`. |
| 16 | Evidence-safe exploitation | ✅ | `sc-pentest-` synthetic resources, minimum-impact, OOB markers, redaction (`common/sensitive.py`), cleanup ledger, evidence content hashes. |
| 17 | Prohibited autonomous behavior | 🟡 | Sandbox + egress allow-list + scope + capability gate + forbidden-ops prevent most; not yet a single codified deny-list policy object. |
| 18 | Independent adversarial review | ✅ | `verify_finding` re-runs the PoC + skeptical LLM (`verify.md`), default-disbelief, OOB-hit confirmation, rejects unproven. |
| 19 | Finding lifecycle | ✅ | Lifecycle states (hypothesis→…→confirmed/rejected→remediated→regression_verified→stale) computed across runs in `memory.merge_run`. |
| 20 | Finding standard | ✅ | Triage/report fields incl. invariant, repro, evidence, PoC, remediation, confidence, blast_radius, attack_chain_position, detection_observations, residual_uncertainty. |
| 21 | Purple-team validation | ✅ | Opt-in `--purple`: `purple_check` assesses prevention/detection/response per confirmed finding (`prompts/purple.md`), honestly reporting "not assessed" when un-observable. |
| 22 | Coverage model | ✅ | Multi-dimensional ledger (`common/coverage.py`): persona × invariant × sensitive-op × object-id × interface, each tested/partial/untested; drives `reflect` + report. No single percentage. |
| 23 | Harness self-defense | ✅ | Scope, redaction, hardened sandbox + egress jail, per-target credential isolation, prompt-injection guardrail (`prompts/_guardrail.md` on every prompt), tamper-evident hash-chained audit log (`common/auditlog.py`), memory-poisoning detection on load. |
| 24 | Quality / research metrics | 🟡 | Benchmark harness (`bench/`) scores FP/FN/recall against seeded + clean apps → `reports/BENCH.md`. Not yet a continuous metrics dashboard. |
| 25 | Completion criteria | ✅ | Loop-until-dry + caps + diminishing-returns `reflect` critic; not payload/checklist exhaustion. |
| 26 | Campaign deliverables | ✅ | Living dossier (`common/dossier.py` → `dossier.json`): authorization record, fingerprint, model, personas, invariant catalog, coverage, hypotheses, findings, attack graph, residual-risk statement. |
| 27 | Review history | n/a | Spec meta-section. |
| 28 | Reference baselines | ✅ | OWASP WSTG/ASVS + curated `knowledge/owasp-remediation.md` grounded into triage. |

## Deliberate behavior choices

- **Authorization is now manifest-based.** `--authorized` on `./assess` synthesizes a
  capability-2 (state-changing, synthetic-data) manifest with a 24h window; `./scan`
  uses capability 1 unless a manifest raises it. Capability 3 (sensitive/risky) and 4
  (destructive) require an explicit `--capability`/`--manifest`; level 4 is prohibited by
  default. The harness can never raise its own level.
- **Halt at pass boundaries.** Within-action breaches (over-capability, out-of-scope,
  forbidden op) are refused immediately by the worker; campaign-wide halts (window expiry,
  kill-switch, accumulated breach) take effect at the next pass via the safety governor.
- **Honesty over assurance.** The report states what was *not* tested (coverage gaps,
  un-assessable detection, blind leads, residual risk) rather than implying the app is secure.

## Still partial / deferred

§4 full multi-layer assertion ledger, §6 queryable knowledge graph, §7 explicit per-persona
baselines, §12 priority-formula scheduler, §17 codified deny-list policy object, and §24 a
continuous metrics dashboard. These are refinements on top of the implemented core.
