# Design ↔ implementation delta

> Tracks the gap between `design/ARCHITECTURE.md` (the end state) and the build, after the
> implementation pass driven by `docs/IMPLEMENTATION_PLAN.md`. Two layers are distinguished:
>
> - **Logic** — the deterministic substance (pure modules in `workers/common/` + `bench/`),
>   provable here by unit tests (`make test`).
> - **Runtime wiring** — Conductor worker registration + workflow DAG edits + prompt
>   injection + a live LLM/target. Provable only against a running server:
>   `make server && make register && make workers && ./assess <target> --manifest <m>`.
>
> After the E11 implementation pass, the feature-exploitation playbook, operation ledger,
> deterministic mandatory hypotheses, completion gate, JS-doc rendering path, and
> reflect→hypothesize chaining are wired into the workflow definitions. The remaining delta
> is a live Conductor/LLM acceptance run proving the external target behavior.

## Per-gap status

| Gap | Logic (unit-proven) | Runtime wiring (server-gated) |
|---|---|---|
| **G1** intel feeds | ✅ `deps.prioritize/top_attempt` (KEV/EPSS/exploit weighting), `deps.parse_kev/parse_epss` — `test_deps_priority`, `test_wiring_cores` | ✅ **folded into `dep_cve_scan`** (`_intel_feeds` live KEV/EPSS + `substrates_version` + `feeds_as_of` → dossier), already in `deep_assess`. The redundant standalone `intel_refresh` worker/taskdef were **removed**. **NVD + GHSA added** (`deps.query_ghsa`/`merge_cve_records`/`nvd_enrich`; GHSA gated on `GITHUB_TOKEN`, NVD backfills severity) — `test_deps_feeds`. |
| **G2** substrate pack + IMDSv2 + replay | ✅ `catalog/substrates.yaml` (8 substrates), `substrates.imds_probe_targets/file_secret_targets/imdsv2_plan/replay_check` — `test_substrates`, `test_wiring_cores` | exploit agent executes the `imdsv2_plan`; `replay_check` run from the sandbox; confirm-and-halt |
| **G3** docs JS-render tier | ✅ `sitemap.urls/security_relevant` + batched Playwright rendering in `rag/tasks.py` | ✅ wired through `docs_ingest(render_js=true, index_docs=true)`; live doc-site acceptance pending |
| **G4** incremental chaining | ✅ `chaining.preconditions/unlocked_objectives/attach` — `test_wiring_cores` | ✅ `evaluate_campaign_progress` persists graph/preconditions → next pass; live chain acceptance pending |
| **G5** provenance types | ✅ `provenance.classify` + `findings.finding(provenance=…)` + `memory._stamp` preserve — `test_provenance` | producers already pass `source_tool`; no further wiring |
| **G6** oracle (living/held-out/adversarial/coverage) | ✅ `oracle.kfold/distill/merge_fixtures`, `score.objective_coverage`; canonical coverage corpus measures **all 31** catalog objectives (0 unmeasured), 9 near-miss negatives; `bench/coverage.py` emits `reports/BENCH.md` offline — `test_oracle`, `test_bench_coverage`, `test_bench_report` | ✅ **held-out split is now genuine**: 2 scored targets (`seeded-vuln-app` + `juice-shop`, `bench/expected/juice-shop.json` = 13 documented challenges across 7 classes) → `holdout.k=2` k-fold; **`oracle_adequate` returns True** (was blocked). Remaining: feed ratified findings into the living corpus from a real run (still authored fixtures, not self-growing). |
| **G7** config versioning/lineage | ✅ **complete** — `config_lineage.*` (content-addressed, versioned, rollback, verify_chain, pinned, `why()`, `snapshot`) — `test_config_lineage` | (used by G8 write-back) |
| **G8** hill-climbing meta-loop | ✅ engine core — `hillclimb.*`, `prompt_units.*`, `trace.*`; `hc_analyze` read-only worker; **trace corpus persisted** (`memory.traces_path` → `state/<fp>/traces.jsonl` via `memory_save`); **write-back loop** `hc_writeback.*` (`decide`/`promote`/`run_cycle`/`apply_ratified`/rollback/`activate` + oracle-adequacy gate) committing reversible `config_lineage` editions; **proposer** (`bench/proposer.py`) + **paired held-out eval** (`bench/eval_runner.py`) — `test_hillclimb`, `test_trace`, `test_trace_persist`, `test_hc_writeback`, `test_proposer` | ✅ the **full cycle is wired**: oracle adequate (2nd scored target `juice-shop`), proposer built (`bench/proposer.py` — deterministic `prompt_units` exemplar-gradient default + gated LLM tier; `prompts/exploit.md` has an opted-in `TACTICS` region), paired held-out eval (`bench/eval_runner.py`), and the orchestrator `hc_writeback.run_cycle` (propose→eval→gated `promote`→reversible lineage), driven by `bench/hc_writeback.py --apply`. Pure loop unit-tested with stubs (`test_hc_writeback` run_cycle cases + `test_proposer`). The only thing left is **running it live** (server + reachable held-out targets + `make register` so a prompt overlay reaches workers) — a live acceptance run, not new logic. |

## Design-decision verification (D1–D18)

Each decision in `design/ARCHITECTURE.md` §21, with where it is enforced and its proof.

| # | Enforced by | Proof |
|---|---|---|
| D1 catalog data spine | `common/catalog.py`, `catalog/objectives.yaml` | `test_catalog` |
| D2 single workflow entry point | `deep_assess.json`, `docs/EXECUTION_MODEL.md` | termination proof (structural) |
| D3 generic engine, profiles/data | `common/profiles.py`, `catalog/substrates.yaml` | `test_profiles`, `test_substrates` |
| D4 adversarial verifier separate | `verify_finding.json`, `prompts/verify.md` | workflow (live) |
| D5 manifest + capability, fail-closed | `common/authz.py` | `test_authz` |
| D6 cross-run memory by fingerprint | `common/memory.py` | `test_memory` |
| D7 provenance on every assertion | `common/provenance.py`, `memory._stamp` | `test_provenance` |
| D8 adapter fidelity tiers | `common/substrates.py`, `common/sitemap.py` | `test_substrates`, `test_wiring_cores` |
| D9 self-improvement gated by ground truth | `hillclimb.accept` (benchmark, not self-verdict) | `test_hillclimb` |
| D10 CVE version-match is a lead | `deps.top_attempt` (version-known only) | `test_deps_priority` |
| D11 substrate own scope, confirm-and-halt | `substrates.replay_check` (bounded/read-only) + `common/halt.py` | `test_wiring_cores`, `test_halt` |
| D12 intel refreshed at loop start, `as_of` | `recon.dep_cve_scan` (`_intel_feeds` + `substrates.version`), `deps.parse_kev/parse_epss` | `test_wiring_cores` |
| D13 cross-tenant proven by *whose* data | `score` precision_failures + `prompts/verify.md` bars | `test_oracle` |
| D14 availability gated, prove the knee | `common/loadknee.py`, `authz.resilience_allowed` | `test_dlp_resilience` |
| D15 detection "not assessed" by default | `prompts/purple.md`, `purple_check.json` | workflow (live) |
| D16 oracle living/held-out/adversarial | `bench/oracle.py` | `test_oracle` |
| D17 statistical + per-class non-regression | `hillclimb.significant/protected_ok/accept` | `test_hillclimb` |
| D18 traces untrusted (H7) | `hillclimb.sanitize_trace`, `trace` controlled-field clustering | `test_hillclimb`, `test_trace` |

## Closing the runtime-wiring delta

The remaining work is orchestration glue, proven by a live run, not new design:

1. `make server && make register && make workers` — registers the `hc_analyze` taskdef and the
   `hc` worker module (added to `WORKER_MODULES`). (Start-of-loop intel is inline in
   `dep_cve_scan`, not a separate task.)
2. Run a live E11 acceptance campaign and verify the operation ledger contains workflow
   definitions, execution IDs, HTTP/INLINE task types, and a CVE attempt or blocked reason.
3. Verify a JS-rendered docs site produces rendered sources and a searchable index.
4. Verify a confirmed privilege gain unlocks and executes a dependent engine-level hypothesis.
5. Run `hc_analyze` over an accumulated trace corpus; iterate the write-back loop offline.

Each is a localized change per the extension model (`design/ARCHITECTURE.md` §20).
