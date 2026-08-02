# PLAN V3: Corner-Case Coverage as a Budget-Bounded, Benchmark-Gated Reservation

## 0. What this document is

`PLAN_V2.md` and the "Feature-Led Deep Hunt with Mandatory Corner-Case Coverage" proposal
are ~90% the same document (same feature graph, same tail buckets, same
`max(2, ceil(35%))` / `max(1, ceil(25%))` reservations, same task list). Rather than run two
divergent plans, **V3 supersedes the tail-coverage portions of both** and does three things
they did not:

1. **Maps every proposed capability against what is already implemented**, so the build does
   not rebuild `deepen.py`, `feature_exercise.py`, or the OOB receipt gate (§2).
2. **Reshapes the "mandatory nook-and-corner" policy** from a completion gate (which reverses
   the ratified stop rule, SPEC §25 / `design/DEEP_EXPLOITATION.md` §6) into a
   **budget-bounded reservation + honest residual-risk accounting** (§3, constraint C1).
3. **Sequences the work cheapest-leverage-first** and ties acceptance to **measured recall on
   seeded tail bugs**, not the process metric "it selected a low-priority feature" (§4, §6).

The diagnosis behind the proposal is correct and is preserved: the harness truncates its
attack surface at several compounding stages and reserves no effort for the long tail. §3
proves it with line references.

---

## 1. Design constraints (the guardrails this plan is accountable to)

These come from the existing architecture and are non-negotiable; every task below is checked
against them.

- **C1 — Coverage is reported, not mandated to exhaustion.** SPEC §25 ends a campaign on
  diminishing risk-adjusted information gain, and `design/DEEP_EXPLOITATION.md` §6 *explicitly
  rejected* a "campaign exhaustion gate." Tail coverage therefore **reserves budget** and
  **reports residual risk**; it never blocks completion on "every reachable tail bucket
  drained." A large app with hundreds of legacy/hidden routes must not be able to run the
  budget to zero.
- **C2 — Magic numbers are data, gated by the benchmark.** Principle D9 (self-improvement
  gated by ground truth) and epic E10 forbid hardcoding tuning constants. The reservation
  fractions, tail-risk weights, and caps are **config-lineage parameters**
  (`workers/common/config_lineage.py`), not literals, and land with benchmark fixtures that
  prove they raise tail-bug recall without regressing FP or cost.
- **C3 — Bound the total.** The campaign is budget-bounded today (`--max-passes`,
  `--max-hypotheses`, `--max-exploit-steps`, `MAX_FEATURE_HYPS`, `MAX_CVE_HYPS`). Every new
  loop/reservation is bounded by an existing or new explicit cap; no unbounded per-bucket fan-out.
- **C4 — Generic engine, specialized by data.** Tail buckets/weights live in catalog/profile
  data and pure modules, never as target-specific logic in the core (principle 1).
- **C5 — The evidence bar is untouched.** New coverage machinery changes *which* features get
  attempted and in *what order* — never how a finding is confirmed. Confirmation stays with
  the deterministic receipt gate + adversarial verifier (principle 2; Theorem 4).

---

## 2. Current-state ledger (the anti-rebuild map)

Verified against the code. **Do not re-implement anything marked BUILT.** Extend at the named seam.

| Proposed capability | Status | Where it lives / what to do instead |
|---|---|---|
| Feature-specific adversarial mutations (injection/RCE/SQLi/traversal ladders, escalation, "discovery ≠ covered") | **BUILT** | `workers/common/deepen.py` + `design/DEEP_EXPLOITATION.md` (Theorems 1–4: no-premature-give-up, breadth-exhaustion, soundness). Extend ladders as **data**, do not rebuild the loop. |
| Agents cannot self-confirm findings | **BUILT** | `workers/common/evidence.py` (`oob_confirmation`, `OOB_VERIFIER="oob_check/v1"`), `deepen.detect_confirmation` (discards agent/sandbox evidence), adversarial `verify_finding`. |
| Mandatory exercise before "done"; features must be operated | **BUILT** | `feature_exercise.evaluate` (completed/blocked/pending, `exercise_rate`) + `prompts/reflect.md` completion gate + `feature_sweep_hypotheses`. |
| Per-objective technique-family coverage | **BUILT** | `feature_exercise.technique_coverage` + `_states_coverage` (survives ledger truncation). |
| Catalog-objective coverage ledger (tested/partial/untested/na/blocked) | **BUILT** | `workers/common/coverage.py` (`build_from_catalog`), wired via `build_coverage`. |
| Residual-risk statement + attack graph | **BUILT** | `workers/common/dossier.py` (`residual_risk`) + `prompts/report.md` §13. |
| Ledger + prefix-sweep cleanup | **BUILT (narrow)** | `httptool/tasks.py` `cleanup_resources` / `sweep_resources`. Missing: final status + absence re-check (see below). |
| Fail-closed network-jailed sandbox (cap-drop, read-only, resource limits, no self-hit) | **BUILT** | `codeexec/tasks.py`, `codeexec/egress.py`, `codeexec/sandbox_sc.py`. Missing: IP-pinning (see below). |
| **Complete, untruncated raw inventory** | **MISSING** | `features.build_inventory` slices `out[:60]` (`features.py:329`) and discards the rest; `sweep_candidates` caps at 40 (`:347`). No persisted graph. **Phase 0.** |
| **Per-feature coverage ledger (discovered→…→verified)** | **WRITTEN BUT DEAD** | `features.feature_coverage` (`features.py:350-392`) computes it over the whole inventory (`max_candidates=10_000`) but is referenced **only in `tests/test_features.py`** — never wired into a worker/workflow. **Phase 0 wires it.** |
| **Tail buckets (legacy/hidden/admin/error/import-export/alternate-interface)** | **MISSING** | No `feature_graph` / `tail_bucket` in code. Prioritization is popularity-of-cues only (`features._PRIO_CUES`, `_prio`). **Phase 1.** |
| **Tail-risk score independent of popularity** | **MISSING** | `_prio` scores *up* the common cues. Nothing scores up rare/undocumented/sparse-auth/legacy. **Phase 1.** |
| **Scheduling reservation for the tail** | **MISSING** | Neither climber reserves budget: `hillclimb.py` tunes prompts *between* runs; `deepen.next_family` round-robins *within* an already-selected sink. **Phase 2.** |
| **Coverage-aware honesty (omitted tail categories listed)** | **MISSING** | Residual risk is not tail-categorized. **Phase 3 (report, not gate — per C1).** |
| **Baseline "happy-path" journey before mutation** | **MISSING** | `explore_agent` is exploratory, not a recorded baseline with object/owner/state capture. **Phase 4 (augment, not replace).** |
| Evidence-state ladder (`source_only`/`source_attested`/`runtime_reproduced`/`runtime_oob_confirmed`) | **MISSING** | Only provenance *kinds* exist (`provenance.py`: observed/documented/source/inferred). **Phase 5.** |
| `--deployment-attestation` input; source-candidate vs live risk split | **MISSING** | No code reference. **Phase 5.** |
| Credential broker / opaque references | **MISSING** | Raw tokens flow through workflow inputs (`assess:169-193`), container env (`SC_IDENTITIES`, `codeexec/tasks.py:144`), and cleartext session files (`session.py:95-111`). **Phase 5.** |
| Marker-secret leak-regression suite | **MISSING** | Closest is `tests/test_httptool.py` (one worker's evidence). No end-to-end planted-secret test across history/reports/logs. **Phase 5.** |
| DNS-rebinding / IP-pinning / explicit RFC1918+link-local egress policy | **MISSING** | `egress_proxy.py:81` `socket.create_connection((host,port))` — no resolved-IP pin/recheck; internal IPs excluded only by *not being allow-listed*. **Phase 5.** |
| `CLEAN` / `RETAINED` / `UNRESOLVED` status + independent absence check | **MISSING** | Output is counts + `residue` (a 404 counts as removed: `httptool/tasks.py:369,:469`). `sc.created()` stores an arbitrary agent-supplied delete URL (`sandbox_sc.py:204-208`). **Phase 5.** |
| `report_quality_gate` (sections/tables/redaction/PDF validation) | **MISSING** | Only `sanitize_md` (JSON_JQ) precedes `report_pdf`. **Phase 6.** |

### The truncation pipeline (why the diagnosis is right)

Raw surface → **LLM 8–15 per category** (`prompts/app_model.md:38`; also `plan.md:30` ~25 checks,
`hypothesize.md:48` "a dozen sharp") → dedupe + priority sort → **top 60** (`features.py:329`) →
**top 40 triaged** (`features.py:347`) → **≤24 deep hypotheses** (`feature_exercise.py:29`). Only
the engine workflow-definition fields are exempt (pinned `prio:100`, `features.py:196,330-335`).
Nothing past each cut is retained. That is the leak the tail work closes.

---

## 3. Reshaped policy: reservation + accounting, not a mandate

The proposal's "the campaign cannot call itself comprehensive while reachable tail buckets remain
only discovered" is replaced by two mechanisms that respect C1:

**3a. Budget reservation (Phase 2).** Per pass, *reserve* a configurable share of the
exploration and exploitation slots for untested tail buckets, drawn from data not literals:

- `tail.explore_reserve` (default `ceil(0.35 * explore_slots)`, min 2) and
  `tail.exploit_reserve` (default `ceil(0.25 * exploit_slots)`, min 1) live in config-lineage
  (C2), so the benchmark can move them and prove the move helps.
- Reservation is **satisfied from the retained tail inventory** (Phase 0) ranked by tail-risk
  (Phase 1), and seeded through the **existing** `build_mandatory_hypotheses` /
  `feature_sweep_hypotheses` seam — not a new scheduler.
- A core feature preempts the reservation only for a verified critical chain or a safety halt
  (mirrors the existing chaining override).
- The reservation is **capped** (C3): it consumes reserved slots within the existing per-pass
  budget; it never adds passes or lifts `--max-hypotheses`.

**3b. Coverage-aware honesty (Phase 3).** The stop rule stays the diminishing-returns critic
(`reflect`) + caps + halt. What changes is the *report*: when the budget is exhausted with tail
buckets still `discovered`/`untested`, the dossier emits an explicit **residual-risk tail
section** enumerating each omitted category and feature with the reason. Budget exhaustion
produces a documented residual, never an implied clean result. This is the honest form of the
proposal's intent and is nearly free given `dossier.residual_risk` already exists.

**Per-feature lifecycle** (Phase 0/1) uses the states the proposal named —
`discovered → reachable → baseline_exercised → adversarially_probed → verified_secure |
verified_vulnerable | blocked` — realized by upgrading the already-written `feature_coverage`
ledger rather than inventing a parallel one.

---

## 4. Phased work

Ordered by leverage-per-unit-effort. Each phase is independently shippable and unit-testable.

### Phase 0 — Cheap wins (do first; ~small, mostly wiring)

- **0.1 Retain the full inventory.** In `features.build_inventory`, stop discarding `out[60:]`:
  return the prioritized slice *and* the complete deduped inventory (or return all, tagged with
  a `truncated_below_rank` marker). Persist it as a run artifact (and, keyed by fingerprint, to
  `workers/common/memory.py`) so it is the durable `feature_graph` substrate.
- **0.2 Wire `feature_coverage`.** Call the existing `features.feature_coverage` from
  `build_coverage` (`recon/tasks.py:1044`) and surface it in `build_dossier` + `prompts/report.md`.
  This delivers the per-feature coverage table with almost no new logic.
- **Acceptance:** a deep run's dossier contains a per-feature coverage table over the *whole*
  inventory, and the persisted inventory count ≥ the number of features actually scheduled.

### Phase 1 — Feature graph + tail classification + tail-risk (`build_feature_graph`, `classify_tail_features`)

- **1.1** Promote the retained inventory into a typed `feature_graph`: keep the concise LLM
  `app_model` for reasoning; the graph is the exhaustive machine record (capability, entry
  points across UI/REST/GraphQL/worker/CLI, inputs, owner/tenant fields, auth middleware,
  sinks, citations, confidence, lifecycle state). Never summarized before persistence; only
  summarized before it is sent to an LLM.
- **1.2** Classify every feature into one or more **tail buckets** (data-driven, C4): core,
  low-traffic, legacy/deprecated/versioned, source-only/hidden/feature-flagged, admin/debug/
  error-handler, import/export/parser, integration/callback/webhook/background-job,
  alternate-interface-for-same-capability.
- **1.3** Add a **tail-risk score** independent of `_prio`: up-weight rare/undocumented/
  source-only, missing/inconsistent auth middleware, sparse tests, complex parsing/serialization,
  privileged side effects, legacy code, config gates, and docs/source/runtime contradictions.
  Weights are config-lineage parameters (C2). Preserve low-confidence source findings as
  **hunt leads**, not drops.
- **Acceptance:** on a fixture app with a hidden/deprecated route, the route appears in the
  graph, is bucketed, and scores above a popular but low-risk core endpoint.

### Phase 2 — Budget-bounded tail reservation (`schedule_feature_campaign`)

- Implement §3a as a pure function over `{feature_graph, feature_coverage, budget}` that returns
  the reserved tail candidates for the pass, then seed them through
  `build_mandatory_hypotheses` / `feature_sweep_hypotheses`. Reward function stays evidence-based
  (new graph edge, baseline completion, cross-persona differential, oracle, concrete blocker) —
  never request/workflow volume. After equivalent no-new-evidence attempts, pivot interface/
  state/identity/encoding (the existing `deepen`/`reflect` pivot, extended to the graph).
- **Acceptance (measured, C2):** on the seeded tail-bug corpus, enabling the reservation raises
  tail-bug recall with FP-rate and cost within the `hillclimb.protected_ok` envelope.

### Phase 3 — Coverage-aware honesty (report, not gate)

- Extend `dossier.residual_risk` + `prompts/report.md` with a **"Corner and Neglected Feature
  Coverage"** section: per-bucket table (discovered / reachable / baseline / adversarially probed
  / verified vulnerable / verified secure / blocked / untested), the high-risk source-only/hidden
  features that were unreachable with exact reasons, and a clean separation of confirmed-live vs
  source-candidate vs untested risk.
- **No completion-gate change** (C1). Stopping remains `reflect` + caps + halt.

### Phase 4 — Baseline journey (`run_baseline_journey`; augment `explore_agent`, do not replace)

- Add a task that, for a selected feature, runs the documented/observed happy path with synthetic
  data and an isolated persona, capturing object IDs, owner/tenant, state transitions, request
  templates, side effects, and cleanup ownership into a `journey_state` — the baseline a Phase 2
  mutation builds on. Reuse `explore_agent`/`playwright_*`; do **not** rip them out (the proposal's
  "replace shallow browse/request" is descoped to "augment").

### Phase 5 — Trust-plane hardening (parallelizable; arguably higher safety priority than 1–4)

Carried from `PLAN_V2.md` §1/§6; a harness that leaks creds or cannot prove cleanup is a worse
failure than one that misses a legacy route. Do in parallel with 1–4.

- **5.1 Credential broker / opaque references.** Pass labels/role/tenant/session handles through
  Conductor; resolve token/secret/cookie values only in a broker immediately before a network
  action. Fixes `assess:169-193`, `SC_IDENTITIES` env (`codeexec/tasks.py:144`), cleartext
  `session.py:95-111`.
- **5.2 Marker-secret leak-regression suite.** Plant a canary secret; assert it never reaches
  workflow history, task output, LLM messages, reports, dossier, SARIF, or logs.
- **5.3 Egress hardening.** Pin the resolved IP in `egress_proxy` and re-check on connect
  (DNS-rebinding defense), add an explicit RFC1918 + link-local + IMDS deny independent of the
  allow-list, validate redirects. Add the missing direct `egress_proxy` allow/deny unit test.
- **5.4 Evidence-state ladder + attestation.** Add `--deployment-attestation`; classify findings
  `source_only` / `source_attested` / `runtime_reproduced` / `runtime_oob_confirmed`; only
  runtime-confirmed affects the live rating. Extend the receipt (`evidence.py`) from OOB-only to a
  binding receipt (objective, feature edge, actor/victim, tenant, artifact, req/resp fingerprint,
  workflow def/exec/task, OOB, cleanup ref) — the in-band typed receipt already flagged TODO in
  `deepen.py`. Tighten `voting.is_dynamically_confirmed` off free-text string matching.
- **5.5 `finalize_cleanup`.** Record a verified creation receipt (not an arbitrary delete URL),
  derive delete paths from trusted target adapters, finalize with delete + prefix sweep +
  **independent list/GET absence check**, and emit one status: `CLEAN` / `RETAINED` /
  `UNRESOLVED`. `--leave-evidence` requires owner, expiry, trusted deletion route, and manual
  removal command.

### Phase 6 — Benchmark fixtures + report gate (`report_quality_gate`)

- **6.1** Add the seeded tail-bug fixtures the proposal lists (hidden/deprecated API missing auth;
  import/export filename traversal; feature-flagged callback SSRF; error-path info leak; background
  retry/rollback authz bug; admin-UI route whose API lacks the same authz; source-only parser
  reachable via a normal upload; vulnerable-but-unreachable dependency). Wire per-class recall/FP
  into `bench/` so E10 measures tail coverage.
- **6.2** `report_quality_gate` before PDF: validate required evidence-state / identity / docs /
  tail-coverage / cleanup sections, Markdown table syntax, schema-based (not regex-only) secret
  redaction, and rendered-PDF layout; reject literal table pipes, missing caveats, secret leakage.
- **Acceptance criterion (outcome, not process):** a deep run demonstrates **measured recall on
  the seeded tail bugs**, exercises selected low-priority features, and for each either produces
  reproducible evidence or records exactly why the feature stayed blocked.

---

## 5. Task/worker changes (proposed → existing → new)

Register each new SIMPLE task (bounded poll/response/execution timeouts, retries) **before** the
workflow edits, per the extension model.

| Proposed task | Action |
|---|---|
| `build_feature_graph` | **New**, but built on Phase 0.1 retained inventory; supersedes/absorbs `build_feature_inventory`. |
| `classify_tail_features` | **New** (Phase 1). Pure module in `workers/common/` + thin worker. |
| `schedule_feature_campaign` | **New** pure function (Phase 2), seeded through existing `build_mandatory_hypotheses`. |
| `run_baseline_journey` | **New** (Phase 4), reusing `explore_agent`/`playwright_*`. |
| `compile_feature_mutations` | **Extend** `feature_exercise.mandatory_hypotheses` / `feature_sweep_hypotheses` — do not build a new engine; ladders are `deepen.py` data. |
| `verify_exploit_receipt` | **Extend** `evidence.py` + `oob_check` into the binding receipt (Phase 5.4). |
| `finalize_cleanup` | **New** wrapper over `cleanup_resources` + `sweep_resources` + absence check + status (Phase 5.5). |
| `report_quality_gate` | **New** (Phase 6.2), replacing the `sanitize_md`-only step. |

---

## 6. Explicitly rejected / deferred (so they are not re-proposed)

| Idea | Disposition | Why |
|---|---|---|
| "Not comprehensive until every reachable tail bucket is drained" as a **completion gate** | **Rejected** | Reverses SPEC §25 diminishing-returns rule; re-proposes the "campaign exhaustion gate" rejected in `design/DEEP_EXPLOITATION.md` §6. Realized as reservation (§3a) + residual accounting (§3b). |
| Hardcoded `35%` / `25%` / tail-risk weights as literals | **Rejected as literals** | Violates C2/D9/E10. Ships as config-lineage data with benchmark fixtures. |
| `journey_explorer` **replacing** `explore_agent`/browser tasks | **Descoped** | Working code; a full stateful per-persona engine is a large build. Ship baseline-journey capture as augmentation (Phase 4). |
| A standalone mutation/technique engine | **Rejected** | Ladders are `deepen.py` data + prompt content per §20; extend, don't rebuild. |
| Acceptance = "it selected a low-priority feature" | **Rejected** | Process metric. Acceptance is measured tail-bug recall (Phase 6). |

---

## 6b. Implementation status

Following `design/GAPS.md`, each item is tracked in two layers: **Logic** (deterministic modules in
`workers/common/`, provable now by `make test`) and **Runtime wiring** (Conductor worker/taskdef/DAG
+ a live LLM/target, provable only by a live acceptance run). Suite: **400 passed, 1 skipped**; ruff clean.

| Phase | Logic (unit-proven) | Runtime wiring |
|---|---|---|
| **0.1** retain full inventory | ✅ `features.build_inventory` tags `rank`/`tail`, no truncation — `test_features::test_build_inventory_retains_full_tail_not_truncated` | ✅ `build_feature_inventory` returns `tail_count`; unchanged DAG position |
| **0.2** wire `feature_coverage` | ✅ now called by `build_feature_graph` over the whole inventory — `test_feature_graph`, `test_dossier` | ✅ threaded `build_feature_graph → build_dossier → report` in `deep_assess.json` |
| **1** feature graph + tail buckets + tail-risk | ✅ `feature_graph.build_graph`/`classify_one` (7 buckets, popularity-independent score, alt-interface/version detection) — `test_feature_graph` | ✅ `build_feature_graph` worker + taskdef, wired pre-dossier |
| **2** budget-bounded reservation | ✅ `feature_graph.reserve` (fractions, mins, bucket diversity, exploit-eligibility, budget bound) + `features.sweep_candidates(include_ids=)` reach-triage — `test_feature_graph`, `test_features` | ⏳ `schedule_feature_campaign` worker + taskdef exist and `feature_triage` accepts `reserved_ids`; **remaining: call `schedule_feature_campaign` inside the pass loop and feed `reserved_ids` back into `feature_triage`** (one localized DAG edit, validated by a live run) |
| **3** tail-coverage honesty (report, not gate) | ✅ `feature_graph.tail_coverage`/`residual_sentence` + `dossier` integration — `test_feature_graph`, `test_dossier` | ✅ dossier carries `tail_coverage`; `report.md` §11 "Corner and Neglected Feature Coverage" |
| **C2** tunables as data | ✅ `tradecraft.mapping`/`number`; weights/fractions overlay-able — `test_feature_graph::test_tunables_overlaid_from_tradecraft_data` | n/a (config-lineage seam) |
| **5.5** cleanup status | ✅ `cleanup_status.finalize`/`summarize` (CLEAN/RETAINED/UNRESOLVED) + dossier stamp — `test_cleanup_status` | ✅ `report.md` §15 renders status; ⏳ independent list/GET absence re-check is a live-request add-on |
| **6.2** report quality gate | ✅ `report_gate.check` (secret redaction, required sections, table syntax, PDF-hostile chars) — `test_report_gate` | ✅ `report_quality_gate` worker + taskdef; ⏳ **remaining: insert it before `report_pdf` in the report DAG** (replaces the `sanitize_md`-only step) |

### Not yet started (larger / higher-risk — recommended next, security-first)

- **Phase 5.1–5.4** trust-plane: credential broker/opaque refs, marker-secret leak-regression suite,
  egress DNS-rebinding/IP-pin + RFC1918/link-local deny, evidence-state ladder + binding receipt.
  (Arguably do before the rest — a harness that leaks creds is worse than one that misses a route.)
- **Phase 4** `run_baseline_journey` (augment `explore_agent`, capture `journey_state`).
- **Phase 6.1** seeded tail-bug benchmark fixtures + per-class recall wiring (the outcome gate).

## 7. Definition of done

For both obvious and neglected features, a deep run can show: how the harness discovered the
capability (persisted, untruncated `feature_graph`); which tail bucket and tail-risk it carries;
how a normal user exercised it (baseline `journey_state`); what source/docs/runtime evidence
described it; which feature-specific mutations were attempted (via the existing `deepen` ladders,
now reachable for reserved tail features); what oracle proved or refuted impact (unchanged
receipt bar); and whether artifacts were cleaned (`CLEAN`/`RETAINED`/`UNRESOLVED`). Budget
exhaustion yields an explicit residual-risk tail section, never an implied clean result. The tail
reservation is proven — on seeded fixtures — to raise recall without regressing FP or cost.
