#!/usr/bin/env python3
"""Offline oracle-readiness report (design §19.2 / P3-4, spec 24).

The benchmark is the oracle the §19 self-improvement loop climbs; its *coverage* bounds what
HC may safely tune. This module answers — WITHOUT a live server — three questions the design
says gate HC:

  1. Which catalog objectives does the benchmark actually MEASURE (>=1 fixture)? On an
     UNMEASURED class HC must NOT auto-tune (it has no ground truth) — §19.2.
  2. Is precision under tension too? (near-miss NEGATIVES vs subtle POSITIVES — adversarial).
  3. What is the HELD-OUT split used for promotion? (k-fold over scored targets, §19.2).

It writes ``reports/BENCH.md``. The live recall/FP table is appended by ``bench/run.py``
(``make bench-live``) when a server + workers + reachable targets exist.

``build_report`` is pure (no IO, no clock) so it is unit-testable; ``main`` does the IO.
"""

from __future__ import annotations

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import score as score_mod  # noqa: E402
import oracle as oracle_mod  # noqa: E402


def load_fixtures(expected_dir: str) -> tuple[list, list]:
    """Union of all fixtures across ``bench/expected/*.json``. Supports both the per-target
    ``expected`` key and the adversarial ``fixtures`` key. Returns ``(positives, negatives)``."""
    positives, negatives = [], []
    for path in sorted(glob.glob(os.path.join(expected_dir, "*.json"))):
        try:
            with open(path) as fh:
                data = json.load(fh)
        except Exception:
            continue
        items = data.get("expected") or data.get("fixtures") or []
        for fx in items:
            (negatives if fx.get("kind") == "negative" else positives).append(fx)
    return positives, negatives


def _scored_targets(targets: list) -> list:
    """Targets that carry ground truth (an ``expected`` file) — the only ones a held-out
    split can be measured over."""
    return [t for t in (targets or []) if t.get("expected")]


def build_report(catalog: list, positives: list, negatives: list, targets: list,
                 *, k: int = 2, as_of: str = "", live_rows: list | None = None) -> str:
    """Render the oracle-readiness Markdown. Pure: all inputs are plain data."""
    cov = score_mod.objective_coverage(positives, catalog)
    by_class: dict = {}
    for obj in catalog or []:
        by_class.setdefault(obj.get("class") or "?", {"objectives": [], "pos": 0, "neg": 0})
        by_class[obj.get("class") or "?"]["objectives"].append(obj.get("id"))
    measured_ids = {e.get("objective_id") for e in positives if e.get("objective_id")}
    measured_classes = {e.get("class") for e in positives if e.get("class")}
    for fx in positives:
        cls = fx.get("class")
        if cls in by_class:
            by_class[cls]["pos"] += 1
    for fx in negatives:
        cls = fx.get("class")
        if cls in by_class:
            by_class[cls]["neg"] += 1

    L = ["# security-conductor — benchmark / oracle readiness (§19.2, spec 24)", ""]
    L.append(f"_Offline oracle-readiness{(' · ' + as_of) if as_of else ''}. "
             "Run `make bench-live` (server + workers + reachable targets) to append live recall/FP._")
    L.append("")

    # 1 — Oracle coverage (the gate on HC auto-tuning).
    L += ["## Oracle coverage (§19.2 / P3-4)", "",
          "Fraction of catalog objectives with >=1 benchmark fixture. **On an UNMEASURED class "
          "the §19 self-improvement loop must NOT auto-tune** — it has no ground-truth signal.", "",
          f"- Catalog objectives: **{cov['total']}**",
          f"- Measured (>=1 fixture): **{cov['measured']}**  (**{round(cov['pct'] * 100, 1)}%**)"]
    if cov["unmeasured"]:
        L.append(f"- **Unmeasured (HC must not tune): {', '.join(cov['unmeasured'])}**")
    else:
        L.append("- **Unmeasured: none — every catalog class is measured. HC may tune any class "
                  "(subject to its other gates).**")
    L.append("")

    # 2 — Per-class fixture inventory.
    L += ["## Per-class fixture inventory", "",
          "| Class | Objectives | Measured | Positives | Near-miss negatives |",
          "|-------|-----------|----------|-----------|---------------------|"]
    for cls in sorted(by_class):
        objs = by_class[cls]["objectives"]
        measured = sum(1 for oid in objs if oid in measured_ids or cls in measured_classes)
        L.append(f"| {cls} | {len(objs)} | {measured}/{len(objs)} | "
                 f"{by_class[cls]['pos']} | {by_class[cls]['neg']} |")
    L.append("")

    # 3 — Adversarial corpus (precision <-> recall tension).
    subtle_pos = [p for p in positives if p.get("id", "").startswith("pos-")]
    L += ["## Adversarial corpus (precision ↔ recall tension, §19.2)", "",
          f"- Subtle positives (must still be found): **{len(subtle_pos)}**",
          f"- Near-miss negatives (must NOT be flagged — a match is a precision failure): "
          f"**{len(negatives)}**", ""]
    if negatives:
        L += ["| Negative | Class | Why it is NOT a finding |",
              "|----------|-------|--------------------------|"]
        for n in negatives:
            L.append(f"| {n.get('id')} | {n.get('class')} | {n.get('why', '')} |")
        L.append("")

    # 4 — Held-out split (promotion eval).
    scored = _scored_targets(targets)
    names = [t.get("name") for t in scored]
    L += ["## Held-out split (promotion eval, §19.2)", "",
          f"K-fold (k={k}) over **{len(scored)} scored target(s)**: a champion is promoted only "
          "on improvement over the holdout fold it never trained on.", ""]
    if len(scored) >= 2:
        L += ["| Fold | Train | Holdout |", "|------|-------|---------|"]
        for i, fold in enumerate(oracle_mod.kfold(names, k)):
            L.append(f"| {i} | {', '.join(fold['train']) or '-'} | {', '.join(fold['holdout']) or '-'} |")
    else:
        L.append(f"> ⚠ Only {len(scored)} scored target ({', '.join(names) or 'none'}). A genuine "
                 "held-out split needs >=2 — add reachable, ground-truthed targets to "
                 "`bench/targets.json` (see the `juice-shop` slot).")
    L.append("")

    # 5 — Live recall / FP (populated by run.py).
    L += ["## Live recall / FP", ""]
    if live_rows:
        L += ["| Target | Expected | Found | Recall | FN rate | FP rate |",
              "|--------|----------|-------|--------|---------|---------|"]
        for r in live_rows:
            s = r["score"]
            L.append(f"| {r['name']} | {s['expected']} | {s['found']} | {s['recall']} | "
                     f"{s['fn_rate']} | {s['fp_rate']} |")
    else:
        L.append("_Not run offline. `make bench-live` scores findings against ground truth._")
    L.append("")
    return "\n".join(L) + "\n"


def _load_catalog(path: str) -> list:
    import yaml  # pyyaml is in the worker venv (common.catalog uses it)
    with open(path) as fh:
        data = yaml.safe_load(fh)
    return data.get("objectives", data) if isinstance(data, dict) else data


def main(targets_path: str = "") -> int:
    targets_path = targets_path or os.path.join(HERE, "targets.json")
    with open(targets_path) as fh:
        cfg = json.load(fh)
    catalog = _load_catalog(os.path.join(ROOT, "catalog", "objectives.yaml"))
    positives, negatives = load_fixtures(os.path.join(HERE, "expected"))
    k = int((cfg.get("holdout") or {}).get("k") or 2)
    # bench/ runs under plain python (not a workflow script), so the wall clock is allowed.
    import datetime
    as_of = "generated " + datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    md = build_report(catalog, positives, negatives, cfg.get("targets", []), k=k, as_of=as_of)
    out_path = os.path.join(ROOT, "reports", "BENCH.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        fh.write(md)
    cov = score_mod.objective_coverage(positives, catalog)
    print(f"wrote {out_path}")
    print(f"oracle coverage: {cov['measured']}/{cov['total']} objectives measured "
          f"({round(cov['pct'] * 100, 1)}%); {len(negatives)} near-miss negatives; "
          f"unmeasured: {cov['unmeasured'] or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
