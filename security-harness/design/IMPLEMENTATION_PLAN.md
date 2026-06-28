# Implementation Plan — to the `design/ARCHITECTURE.md` end-state

> **Source of truth:** `design/ARCHITECTURE.md` (the target design). This plan tracks the
> **delta** between that design and the current implementation, as a checklist.
> **Companion:** `CONFORMANCE.md` (§-by-§ status), `design/ROADMAP.md` (epic history).
>
> **Status legend:** `[x]` built · `[~]` partial · `[ ]` not started. Section references
> (e.g. §19.2, D16) point into `design/ARCHITECTURE.md`.
>
> **The one rule that governs sequencing:** *the oracle is the foundation; HC is the engine.*
> Phase 3 (the oracle + trace corpus + config versioning) is a **hard prerequisite** for any
> hill-climbing **write-back** (Phase 4c+). Building self-modification on a skeletal benchmark
> violates D16 and actively degrades the engine.

---

## Phase 0 — Baseline (already built)

The entire assessment engine is production-grade. Recorded here so the plan is complete.

- [x] **L1 Orchestration** — 15 workflows, 10 workers, `assess`/`scan`/`retest.py`, 27 taskdefs
- [x] **L3 Reasoning** — 14 prompts + `register.sh` injection
- [x] **L4 Action** — `http_request` (capability-gated, halt-aware), `code_exec` (hardened sandbox), `browser` (Playwright), `load_probe` (knee), `sast`, `oob` (collaborator)
- [x] **L5 Epistemics (§12)** — separate adversarial verifier, per-class evidence bars, OOB confirmation, content-hash integrity, contradiction detection
- [x] **L6 Safety/Authz (§10)** — manifest + capability levels 0–4 + `forbids`, scope enforcement, halt conditions, safety governor, guardrail preamble, append-only hash-chain audit log, leave-clean cleanup
- [x] **L7 Assurance (§18)** — per-fingerprint memory + stale-on-release, coverage ledger, dossier, OWASP/ASVS compliance rollup, regression + `retest`
- [x] **§13 Data-exfiltration chain** — `CONF-*` objectives, `sensitive.classify` (6 DLP buckets), minimal-exposure halt
- [x] **§16 Availability chain** — `AVAIL-*` objectives, `loadknee` bounded-ramp knee analysis, resilience capability gate (off by default)
- [x] **§17 Detection/purple (§17)** — `purple_check` workflow, `DETECT-COVERAGE` objective, "not assessed" honest default
- [x] **Catalog spine (§6/§7)** — `catalog/objectives.yaml`, profiles (`profiles/conductor.json`), CVE-via-OSV chain (§14, partial — see G1)

---

## The delta to close

| # | Artifact | Status | Section |
|---|---|---|---|
> **Status after the implementation pass (see `design/GAPS.md`):** every gap's **logic is
> implemented and unit-proven** (`make test` green). The residual `[~]` is
> **runtime wiring** (Conductor workflow-DAG edits + live LLM/target run), gated on a server.
>
> **⚠ Live-run RCA (`docs/RCA-product-exploitation-gap.md`):** a real run against
> your-conductor.example.com tested the **management API**, not Conductor's **engine** —
> `code_exec`/`startWorkflow`/`HTTP_TASK`/`INLINE` were used **0×**; CVE leads = 5, attempted
> = 0. Root cause: "use the app's own features" is vision, not a *forced + measured* capability,
> and the deeper depth is unwired (RC-C). **New epic E11** (in the RCA) is now the top priority:
> a profile-encoded feature-exploitation playbook, a forcing function + measure, the live
> wiring, and visibility — so the harness actually drives the product (define+run workflows,
> abuse task types, exploit CVEs), not just its REST surface.

| G1 | Intel refresh: KEV/EPSS + exploit-availability + `feeds_as_of` | `[x]` **folded into `dep_cve_scan`** (`_intel_feeds`, live, wired; standalone worker removed) · `[ ]` NVD/GHSA identity (still OSV-only, P1-2) | §5, §14, D12 |
| G2 | Substrate metadata reference pack + IMDSv2 handshake + replay-validation | `[x]` pack+plan+replay logic · `[~]` exploit-exec wiring | §15, D11 |
| G3 | `docs_ingest` JS-rendered crawl tier + vector retrieval | `[x]` logic + workflow wiring · `[~]` live-site acceptance | §11 |
| G4 | Incremental attack-graph chaining (machine-driven preconditions) | `[x]` logic + reflect→hypothesize wiring · `[~]` live-chain acceptance | §5, §8 |
| G5 | Distinct provenance types (documented/source/inferred) | `[x]` **complete** (`provenance.py`) | §12 |
| G6 | The oracle: living + held-out + adversarial + coverage metric | `[x]` engine (`bench/oracle.py`) + full 31/31 catalog coverage corpus + `bench/coverage.py`→`reports/BENCH.md` · `[x]` held-out split genuine — **2 scored targets** (`seeded-vuln-app` + `juice-shop`, `holdout.k=2`); `oracle_adequate` True · `[~]` living corpus not yet self-growing from real runs | §18, §19.2 |
| G7 | Config versioning / lineage (catalog/prompts/profiles) | `[x]` **complete** (`config_lineage.py`) | §19 H5 |
| G8 | **The §19 hill-climbing meta-loop** | `[x]` engine core + read-only worker · `[~]` write-back/live | §19 |

---

## Phase 1 — Knowledge & intel completeness (L2)

*Independent of HC; also enriches the trace corpus HC will later consume.*

- [~] **P1-1 — Intel-refresh subsystem (§5, D12).** Start-of-loop step refreshing KEV + EPSS, stamping `feeds_as_of` into dossier/report.
  - [x] **folded into `dep_cve_scan`** (`recon.tasks._intel_feeds` — live KEV/EPSS + `substrates_version`); the redundant standalone `intel_refresh` worker/taskdef were removed
  - [x] runs in the wired UNDERSTAND phase (no separate task to wire)
  - [x] `feeds_as_of` threaded into `dossier.py`
  - [ ] NVD/GHSA identity (still OSV-only — P1-2)
  - *Done when:* a run records `feeds_as_of`; KEV/EPSS reflected in prioritization. **(Done for KEV/EPSS.)**
- [~] **P1-2 — Multi-feed CVE identity + weaponization (§14).** Extend `deps.py` / `dep_cve_scan` beyond OSV.
  - [x] **NVD + GHSA** — `deps.query_ghsa` (GitHub Advisory DB, package-based, version-unknown historical leads) + `deps.merge_cve_records` (OSV ∪ GHSA, dedupe by CVE id, version_known OR) + `deps.nvd_enrich` (NVD CVSS severity backfill by CVE id). Worker fetchers `_ghsa_fetch`/`_nvd_fetch`; GHSA gated on `GITHUB_TOKEN` (degrades to OSV-only); `feeds_used` reported. **Tests:** `tests/test_deps_feeds.py` (6).
  - [x] CISA KEV + FIRST EPSS (in-the-wild) — `_intel_feeds` (P1-1)
  - [ ] exploit-availability (Exploit-DB / Metasploit / Nuclei)
  - [x] prioritization = reachable × version-matched × severity × (KEV/EPSS) × exploit-available — `deps.prioritize`
  - *Done when:* KEV-listed, exploit-available, reachable CVEs are tried first. **(Identity = OSV+GHSA; severity = OSV/GHSA/NVD; weaponization index still pending.)**
- [x] **P1-3 — Substrate metadata reference pack (§15, G2).** → `catalog/substrates.yaml`, `workers/common/substrates.py`, `tests/test_substrates.py`
  - [x] `catalog/substrates.yaml` — AWS IMDSv1/v2 (+ handshake data + IPv6), GCP, Azure, OCI, Alibaba, DO, k8s, host; versioned + `as_of`
  - [x] loader: `imds_probe_targets` (HTTP/SSRF, cloud only) **vs** `file_secret_targets` (file-read: k8s SA token, `/proc/self/environ`, `~/.aws/credentials`…) — faithful to §15's two technique families; `infer()` fingerprints the substrate
  - [ ] wired into the start-of-loop refresh — deferred to **P1-1** (`as_of` stamping into the report)
  - *Done (data layer):* infra-chain probes are data-driven across all 8 substrates; a new cloud is a YAML edit (D1/D3). **Proof:** `pytest tests/test_substrates.py` 7/7; full suite 144.
- [ ] **P1-4 — IMDSv2 handshake + replay-validation (§15, D11).**
  - [ ] SSRF technique performs the PUT-token dance
  - [ ] bounded, read-only "replay-from-outside" step proves an extracted credential is live
  - [ ] confirm-and-halt at the substrate boundary
  - *Done when:* an extracted cred is confirmed by replay, then the run halts at the boundary.
- [ ] **P1-5 — `docs_ingest` JS-render tier (§11, G3).**
  - [ ] Playwright crawl over the target's sitemap (reuse the L4 browser)
  - [ ] vector-indexed retrieval wired into reasoning prompts
  - *Done when:* a JS-rendered doc site yields real doc text, not a nav shell.

---

## Phase 2 — Loop strategic completeness (L5, L7, the loop)

*Independent of HC; completes the kill-chain behavior and provenance.*

- [ ] **P2-1 — Incremental attack-graph chaining (§5/§8, G4).**
  - [ ] `reflect_pass` emits structured *chaining preconditions* (confirmed finding → pivot/precondition)
  - [ ] preconditions feed the next `hypothesize`; attack graph assembled incrementally
  - [ ] `dossier.build_attack_graph` reads the incremental graph
  - *Done when:* a confirmed finding in pass N produces a dependent hypothesis in pass N+1.
- [ ] **P2-2 — Distinct provenance types (§12, G5).**
  - [ ] set `documented` (docs), `source` (SAST), `inferred` (heuristic) — not just `observed`
  - *Done when:* contradiction detection distinguishes documented-vs-observed correctly.

---

## Phase 3 — The oracle & its prerequisites (§18, §19.2) — *the foundation*

**Hard gate before any HC write-back (Phase 4c+).**

- [ ] **P3-1 — Living benchmark (§19.2, D16).**
  - [ ] mechanism to distill a human-ratified real finding into a permanent fixture
  - [ ] `as_of`-stamped fixture lineage
  - *Done when:* a ratified finding becomes a regression fixture that must keep passing.
- [ ] **P3-2 — Held-out split / K-fold (§19.2).**
  - [ ] promotion-eval set the proposer never sees; cross-target evaluation
  - [ ] expand `bench/targets.json` for genuine multi-target diversity
  - *Done when:* promotion is measured only on held-out targets.
- [~] **P3-3 — Adversarial near-miss corpus (§19.2).**
  - [x] paired "looks-vuln-but-isn't" negatives (9, spanning confidentiality/client/availability/authz/infra/crypto/auth) + "subtle real" positives (4) in `bench/expected/adversarial.json`
  - [ ] broaden to a near-miss pair for every high-value class
  - *Done when:* precision and recall are both under tension. **(Under tension now; broaden coverage.)**
- [x] **P3-4 — Benchmark coverage metric (§19.2).**
  - [x] `bench/score.objective_coverage` reports % of catalog objectives with ≥1 fixture + flags unmeasured classes; `bench/coverage.py` renders it to `reports/BENCH.md` **offline** (`make bench`)
  - [x] canonical coverage corpus (`bench/expected/catalog-coverage.json`) measures **all 31** objectives (0 unmeasured); guarded by `tests/test_bench_report.py` so a new catalog entry without a fixture fails CI
  - *Done:* unmeasured classes are flagged (HC must not tune them); currently none.
- [x] **P3-5 — Structured trace corpus (§19.2).**
  - [x] verdict-with-reasons records — `trace.from_findings` builds one record per confirmed/rejected/blind verdict (objective_id, outcome, evidence_bar = class, reason, finding_sig); clustering keys off controlled fields only (H7).
  - [x] queryable per-run trace store — `memory.traces_path(fp)` → `state/<fingerprint>/traces.jsonl`; `memory_save` persists the run's traces there (best-effort), so the corpus grows across runs (recurrence = signal, H4). `hc_analyze` already loads via `trace.load`/`trace_path`.
  - *Done when:* each verdict records *why*; corpus is machine-readable. **(Done — `tests/test_trace_persist.py`. Now mining real persisted traces, not a mirrored corpus.)**
- [x] **P3-6 — Config versioning & lineage (§19 H5, G7).** → `workers/common/config_lineage.py`, `tests/test_config_lineage.py`
  - [x] `version` + content-hash on `catalog/objectives.yaml`, `prompts/*`, `profiles/*` — `snapshot()` baselines the live tree (proven: 15 artifacts → one verifiable lineage)
  - [x] content-addressed editions + a `config_lineage` store + `rollback_target` + `verify_chain` + pinned (model, benchmark, seed) + provenance + benchmark before/after + `why()` reproducibility
  - *Done:* every config edition is attributable (provenance), reproducible (`why()` + pins), reversible (`rollback_target`), and tamper-evident (`verify_chain`). Safety/authz rejected as a surface (H2). **Proof:** `pytest tests/test_config_lineage.py` green.

---

## Phase 4 — The hill-climbing meta-loop (§19) — *the centerpiece, staged*

*P4-a is safe to start once P3-5 exists; P4-c+ (write-back) is gated on Phase 3 complete.*

- [ ] **P4-a — Read-only analysis agent** (the safe half) — §19.2, §19.5 steps 1–3.
  - [ ] mines trace corpus + oracle, clusters recurring failure signatures
  - [ ] applies the diagnosis→surface map; emits **proposals only** (no write-back)
  - [ ] new `hc_analyze` worker/workflow + proposal schema
- [ ] **P4-b — Statistical evaluation harness** — §19.5 steps 4–5, D17.
  - [ ] N seeded paired runs on held-out; bootstrap CI / significance test
  - [ ] per-class non-regression; attempt-rate metric; deterministic ground truth where possible
- [~] **P4-c — Champion/challenger + gated write-back** — §19.4, §19.5 step 6, H2/H5/H6. → `workers/common/hc_writeback.py` (`decide`/`promote`/`apply_ratified`/`should_rollback`/`rollback_plan`/`activate` + `oracle_adequate`), runner `bench/hc_writeback.py` (`--selftest` + read-only corpus analysis), `tests/test_hc_writeback.py` (13).
  - [x] single-surface diffs committed as content-addressed lineage editions (via `config_lineage.commit`)
  - [x] auto-tune profiles/prompts (benchmark-gated) vs human-ratify catalog/evidence bars vs **never** safety — `decide` composes `hillclimb.gate`/`accept`/`autonomy` + `needs_rebaseline`
  - [x] versioned champion + `should_rollback`/`rollback_plan` (H5 auto-revert on protected-metric regression); shadow promotion (`promote(shadow=True)`) + `activate`
  - [x] **oracle-adequacy gate (D16/§19.2):** auto-adopt requires a *measured* class **and** ≥2 scored targets, else downgraded to human ratification — so it cannot self-modify on a thin oracle
  - [x] **2nd scored target added** (`juice-shop`, `bench/expected/juice-shop.json`, 13 challenges / 7 classes) → `holdout.k=2`, `oracle_adequate` now **True**. The §19.2 keystone is met.
  - [x] **proposer + paired eval + cycle wired** — `hc_writeback.run_cycle` (propose→eval→gated promote→reversible lineage); `bench/proposer.py` (deterministic `prompt_units` exemplar-gradient + gated LLM tier; `prompts/exploit.md` `TACTICS` region opted in); `bench/eval_runner.py` (held-out scan+score); driven by `bench/hc_writeback.py --apply`. Loop unit-tested with stubs (`test_hc_writeback` run_cycle + `test_proposer`).
  - [ ] **live** autonomous adoption — run `--apply` against a server with ≥2 reachable held-out targets (+ `make register` so a prompt overlay reaches workers). This is a live acceptance run; all logic is in place.
- [ ] **P4-d — Search strategy** — §19.6.
  - [ ] population / Pareto frontier; bandit / successive-halving eval budget; bounded annealing (downhill only on unprotected dims)
- [ ] **P4-e — Text-surface optimization** — §19.7.
  - [ ] OPRO/DSPy/TextGrad-style proposer fed concrete failing exemplars
  - [ ] prompt decomposition into frozen method-core + tunable tactics block
- [ ] **P4-f — Optimizer hardening** — §19.3 H7, §19.8, D18.
  - [ ] H7: traces untrusted in the proposer
  - [ ] signed lineage + pinned (model, benchmark, seed); re-baseline on model change
  - [ ] shadow promotion; graded autonomy gate
  - [ ] novelty channel (propose new catalog classes from unmapped finding clusters)
- [ ] **P4-g — Cold-start & cross-deployment transfer** — §19.9.
  - [ ] bootstrap traces from the benchmark (curriculum)
  - [ ] cross-deployment champion transfer (generic gains propagate; per-target quirks stay in profiles)

---

## Phase 5 — Invariant verification, tests, docs (cross-cutting)

- [ ] **P5-1** — Unit/integration tests per new module (mirror `tests/test_*`); benchmark-gated regression on the Juice-Shop baseline
- [ ] **P5-2** — Verify all 18 design decisions (D1–D18) hold in code; close any drift
- [ ] **P5-3** — Update `CONFORMANCE.md` (§-by-§ implemented/deferred) and write the separate **gaps doc** (design↔impl delta)

---

## Sequencing

```
Phase 1 ─┐
Phase 2 ─┤── parallel, independent (also enrich the trace corpus)
         │
Phase 3 ─┴──► HARD GATE ──► Phase 4c+ (HC write-back)
  (oracle + trace corpus + config versioning)
         └──► Phase 4a (read-only proposals) may start once P3-5 exists
```

**Recommended first increment:** P3-5 (structured trace corpus) + P3-6 (config versioning) +
P4-a (read-only analyzer). Low-risk, unlocks everything downstream, and delivers visible value
(the system tells you *how it would improve itself*) before any autonomy is wired.
