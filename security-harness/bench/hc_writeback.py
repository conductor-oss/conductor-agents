#!/usr/bin/env python3
"""§19 hill-climbing write-back runner (out-of-band — NOT part of an assessment campaign).

Three modes:
  (default)    Read-only: mine the persisted trace corpus (state/<fp>/traces.jsonl, produced by
               memory_save) -> recurring failure signatures (H4) -> proposals (diagnosis->surface),
               then project each against the §19.4 surface gate + the D16/§19.2 oracle-adequacy
               gate, and print what WOULD happen (auto-adopt vs human-ratify vs blocked). It does
               NOT evaluate or apply.
  --apply      The LIVE cycle (needs a server + reachable held-out targets): proposer produces the
               actual edit (deterministic prompt_units gradient by default; the LLM tier when
               ANTHROPIC_API_KEY is set), each challenger is evaluated paired against the champion
               on the held-out split (oracle.kfold), and the gated promote commits reversible
               lineage editions for the auto-adopted ones (-> reports/lineage.json).
  --distill    The living-oracle RATIFICATION HOOK: drain HUMAN-RATIFIED confirmed findings
               (``ratified: true``) from reports/ into bench/expected/ratified.json, forgetting-
               guarded — turning a real win into a permanent regression fixture (grows the ground
               truth the adequacy gate reads). No server needed. --apply runs this automatically at
               the end of the cycle; --ratify-all is the operator's blanket H6 assertion.
  --selftest   Exercise the full write-back mechanism (promote / apply_ratified / rollback_plan
               / run_cycle) with stub proposer+eval + a synthetic oracle, proving the loop promotes
               a winning challenger, skips a declined proposal, and the gates fire correctly.

Now that a 2nd scored target exists the adequacy gate permits auto-adoption; on a thin oracle it
still DOWNGRADES every otherwise-auto change to human ratification (D16/H1). Usage:
    python bench/hc_writeback.py            # analyze the live trace corpus (read-only projection)
    python bench/hc_writeback.py --apply    # live cycle: propose -> eval held-out -> gated promote
    python bench/hc_writeback.py --distill  # drain ratified findings into the living oracle
    python bench/hc_writeback.py --selftest
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "workers"))
import coverage as coverage_mod  # noqa: E402
import distill_findings as distill_mod  # noqa: E402
import oracle as oracle_mod  # noqa: E402
import proposer as proposer_mod  # noqa: E402
import score as score_mod  # noqa: E402
from common import config_lineage, hc_writeback as wb, hillclimb, trace  # noqa: E402

LINEAGE_PATH = os.environ.get("LINEAGE_PATH", os.path.join(ROOT, "reports", "lineage.json"))
REPORTS_DIR = os.environ.get("REPORTS_DIR", os.path.join(ROOT, "reports"))


def distill_hook(as_of: str, *, ratify_all: bool = False) -> int:
    """Post-run RATIFICATION HOOK (§19.2 living oracle): drain HUMAN-RATIFIED confirmed findings from
    the reports into bench/expected/ratified.json, forgetting-guarded. Grows the measured-class /
    scored-target ground truth the oracle-adequacy gate reads — so a real win becomes a permanent
    regression fixture and the engine climbs toward reproducing it. Best-effort: never raises (a
    distill failure must not fail the HC ceremony)."""
    try:
        res = distill_mod.distill_ratified_reports(
            REPORTS_DIR, into=distill_mod.DEFAULT_INTO, as_of=as_of, ratify_all=ratify_all)
    except Exception as exc:
        print(f"  · distill hook skipped: {exc}")
        return 0
    if res.get("ratified", 0) == 0:
        print(f"  · distill: 0 ratified of {res.get('scanned', 0)} confirmed finding(s) — "
              f"mark findings ratified:true (or use --distill --ratify-all) to grow the oracle.")
        return 0
    print(f"  ✓ distilled {res['ratified']} ratified finding(s); added {len(res['added'])} new "
          f"fixture(s) to the living oracle (corpus now {len(res['corpus'])}).")
    if res["added"]:
        print("    added:", ", ".join(res["added"]))
    return len(res["added"])


def build_benchmark() -> dict:
    """The oracle descriptor the adequacy gate reads: which catalog classes are measured, and how
    many SCORED (ground-truthed) targets exist for a held-out split."""
    catalog = coverage_mod._load_catalog(os.path.join(ROOT, "catalog", "objectives.yaml"))
    positives, _ = coverage_mod.load_fixtures(os.path.join(HERE, "expected"))
    cov = score_mod.objective_coverage(positives, catalog)
    try:
        cfg = json.load(open(os.path.join(HERE, "targets.json")))
        scored = sum(1 for t in cfg.get("targets", []) if t.get("expected"))
    except Exception:
        scored = 0
    return {"objective_coverage": cov, "scored_targets": scored, "name": "bench"}


def analyze(state_dir: str, benchmark: dict) -> int:
    corpora = glob.glob(os.path.join(state_dir, "*", "traces.jsonl"))
    print(f"=== HC write-back analysis (read-only) — {len(corpora)} trace corpus/corpora ===")
    print(f"oracle: {benchmark['objective_coverage']['measured']}/{benchmark['objective_coverage']['total']} "
          f"classes measured; {benchmark['scored_targets']} scored target(s) for the held-out split")
    if benchmark["scored_targets"] < 2:
        print("  ⚠ AUTO-ADOPTION BLOCKED: <2 scored targets — every proposal routes to human ratification (D16).")
    total = 0
    for path in corpora:
        recs = [hillclimb.sanitize_trace(r) for r in trace.load(path)]   # H7 sanitize
        recurring = trace.recurring(recs, min_count=2)                    # H4 corroboration
        proposals = hillclimb.propose(recurring, benchmark)
        if not proposals:
            continue
        print(f"\n  {path}  ({len(recurring)} recurring signatures -> {len(proposals)} proposals):")
        for p in proposals:
            gate = hillclimb.gate(p["surface"])
            adq, why = wb.oracle_adequate(benchmark, p["surface"], p.get("objective_id"))
            projected = "auto-adopt (if it clears the benchmark)" if (gate == "auto" and adq) else "human-ratify"
            total += 1
            print(f"    - [{p['surface']}/{gate}] {p.get('objective_id')}: {p['diagnosis']}")
            print(f"        -> {projected}" + ("" if adq else f"  (oracle: {why})"))
    if total == 0:
        print("\n  no recurring signatures yet — run more campaigns to grow the corpus (memory_save persists it).")
    return 0


def _load_store() -> list:
    try:
        return json.load(open(LINEAGE_PATH))
    except Exception:
        return []


def _save_store(store: list) -> None:
    os.makedirs(os.path.dirname(LINEAGE_PATH), exist_ok=True)
    with open(LINEAGE_PATH, "w") as fh:
        json.dump(store, fh, indent=2)


def apply_cycle(state_dir: str, benchmark: dict, as_of: str, *, current_model: str) -> int:
    """LIVE write-back cycle (needs a server + reachable held-out targets). Mines the trace
    corpus, asks the proposer for edits, evaluates each challenger on the held-out split, and
    runs the gated promote — committing reversible lineage editions for the auto-adopted ones."""
    cfg = json.load(open(os.path.join(HERE, "targets.json")))
    scored = [t for t in cfg.get("targets", []) if t.get("expected")]
    if len(scored) < 2:
        print(f"⚠ only {len(scored)} scored target(s); the oracle-adequacy gate would downgrade "
              f"every auto change to ratify. Add a 2nd scored target first.")
        return 1
    k = int((cfg.get("holdout") or {}).get("k") or 2)
    holdout = oracle_mod.holdout(scored, k=k, fold=0)            # the split the proposer never trained on
    expected_by_target = {t["name"]: json.load(open(os.path.join(ROOT, t["expected"])))["expected"]
                          for t in holdout}
    import eval_runner  # noqa: E402  (imports run.py -> needs the CLI; only on the live path)
    # Prompts are injected into taskdefs at register time, so a *prompt* overlay only reaches the
    # live workers if we re-register between write and scan (eval_runner.make_eval_fn docstring).
    # The prompt surface is the ONLY auto-tunable one, so without this hook the cycle can never
    # promote anything. Gate it behind HC_REREGISTER=1 so the offline --selftest / CI path stays pure.
    reregister = (lambda _p: subprocess.run(["make", "register"], cwd=ROOT, check=False)) \
        if os.environ.get("HC_REREGISTER") == "1" else None
    eval_fn = eval_runner.make_eval_fn(expected_by_target, on_apply=reregister)
    # Proposer: deterministic by default; prefer the LLM tier when a key is configured.
    propose_fn = proposer_mod.llm_proposer() if proposer_mod._anthropic_client() \
        else proposer_mod.deterministic_proposer()
    traces = [r for path in glob.glob(os.path.join(state_dir, "*", "traces.jsonl")) for r in trace.load(path)]
    print(f"=== HC write-back cycle (LIVE) — {len(traces)} traces, held-out: {[t['name'] for t in holdout]} ===")

    store = _load_store()
    out = wb.run_cycle(store, traces, propose_fn=propose_fn, eval_fn=eval_fn,
                       surface_path=proposer_mod.surface_path, champion_model=current_model,
                       current_model=current_model, benchmark=benchmark, as_of=as_of,
                       holdout_targets=holdout, n_runs=int(os.environ.get("HC_N_RUNS", "3")))
    _save_store(store)
    print(f"proposals: {out.get('proposals', 0)} | promoted: {len(out['promoted'])} | "
          f"ratify: {len(out['pending_ratification'])} | rejected: {len(out['rejected'])} | "
          f"skipped: {len(out.get('skipped', []))}")
    for p in out["promoted"]:
        print(f"  ✓ AUTO-ADOPTED {p['surface']}:{p['path']} v{p['version']} — {p['why']}")
    for p in out["pending_ratification"]:
        print(f"  ⟳ RATIFY {p['surface']}:{p['path']} — {p['reason']}")
    for s in out.get("skipped", []):
        print(f"  · skipped {s['surface']}/{s.get('objective_id')} — {s['why']}")
    # Living-oracle ratification hook: grow the ground truth AFTER the promote, so a freshly-distilled
    # finding can never gate its own adequacy this cycle — it takes effect on the next one.
    distill_hook(as_of)
    return 0


def _selftest() -> int:
    bench_ok = {"objective_coverage": {"unmeasured": []}, "scored_targets": 2, "name": "b"}
    bench_thin = {"objective_coverage": {"unmeasured": []}, "scored_targets": 1, "name": "b"}
    before = [{"recall": .60, "fp_rate": .05, "per_class": {"infra": .6}}] * 5
    after = [{"recall": r, "fp_rate": .05, "per_class": {"infra": .75}} for r in (.74, .76, .73, .77, .75)]

    def ch(surface, **kw):
        return {"id": f"ch-{surface}", "surface": surface, "objective_id": "INFRA-SSRF",
                "path": {"prompt": "prompts/exploit.md", "catalog": "catalog/objectives.yaml"}.get(surface, "x"),
                "diagnosis": "weak technique", "content": "edited", "before_runs": before, "after_runs": after, **kw}

    store = []
    out = wb.promote(store, [ch("prompt")], champion_model="m", current_model="m", benchmark=bench_ok, as_of="t1")
    assert len(out["promoted"]) == 1, out
    assert config_lineage.head(store, "prompt", "prompts/exploit.md")["provenance"]["shadow"] is True
    assert config_lineage.verify_chain(store)["ok"]
    # thin oracle: nothing applied, queued for ratification
    s2 = []
    o2 = wb.promote(s2, [ch("prompt")], champion_model="m", current_model="m", benchmark=bench_thin, as_of="t1")
    assert o2["promoted"] == [] and len(o2["pending_ratification"]) == 1 and s2 == []
    # catalog -> ratify; safety -> reject
    o3 = wb.promote([], [ch("catalog")], champion_model="m", current_model="m", benchmark=bench_ok, as_of="t1")
    assert len(o3["pending_ratification"]) == 1
    # rollback plan after a second edition
    s4 = []
    config_lineage.commit(s4, surface="prompt", path="p", content="v1", as_of="t1")
    config_lineage.commit(s4, surface="prompt", path="p", content="v2", as_of="t2")
    assert wb.rollback_plan(s4, "prompt", "p")["version"] == 1

    # run_cycle end-to-end with STUB proposer + eval (proves the loop, no server): two recurring
    # 'rejected' traces -> a prompt proposal -> challenger beats champion on the held-out split.
    traces = [{"objective_id": "INFRA-RCE-INJECTION", "outcome": "rejected", "evidence_bar": "exec",
               "reason": "SpEL payload filtered"}] * 2
    base = {"recall": .60, "fp_rate": .05, "per_class": {"infra": .6}}
    better = {"recall": .80, "fp_rate": .05, "per_class": {"infra": .8}}
    cyc = wb.run_cycle([], traces,
                       propose_fn=lambda p, path: "edited tactics",
                       eval_fn=lambda overlay, t: (better if overlay else base),
                       surface_path=lambda s, o: "prompts/exploit.md" if s == "prompt" else None,
                       champion_model="m", current_model="m", benchmark=bench_ok, as_of="t1",
                       holdout_targets=[{"name": "x"}], n_runs=5)
    assert cyc["proposals"] >= 1 and len(cyc["promoted"]) == 1, cyc
    # a proposer that declines (None) -> the proposal is skipped, nothing promoted
    cyc2 = wb.run_cycle([], traces, propose_fn=lambda p, path: None,
                        eval_fn=lambda overlay, t: (better if overlay else base),
                        surface_path=lambda s, o: "prompts/exploit.md",
                        champion_model="m", current_model="m", benchmark=bench_ok, as_of="t1",
                        holdout_targets=[{"name": "x"}], n_runs=5)
    assert cyc2["promoted"] == [] and len(cyc2["skipped"]) >= 1, cyc2

    print("selftest OK — auto-adopt commits a shadow edition (chain intact); thin oracle & catalog "
          "downgrade to ratification with nothing written; rollback target resolves to the prior "
          "edition; run_cycle promotes a winning stub challenger and skips a declined proposal.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "--selftest":
        return _selftest()
    state_dir = os.environ.get("STATE_DIR", os.path.join(ROOT, "state"))
    if argv and argv[0] == "--distill":
        import datetime
        as_of = "distilled " + datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
        print(f"=== HC living-oracle distill (ratification hook) — reports: {REPORTS_DIR} ===")
        distill_hook(as_of, ratify_all="--ratify-all" in argv)
        return 0
    if argv and argv[0] == "--apply":
        import datetime
        as_of = "live " + datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
        model = os.environ.get("HC_MODEL", "claude-opus-4-8")
        return apply_cycle(state_dir, build_benchmark(), as_of, current_model=model)
    return analyze(state_dir, build_benchmark())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
