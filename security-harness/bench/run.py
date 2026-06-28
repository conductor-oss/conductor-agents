#!/usr/bin/env python3
"""Benchmark harness (spec section 24): run scans against ground-truth apps and
report FP / FN / recall quality metrics. Requires a running Conductor server +
workers + the target apps. Writes reports/BENCH.md.

Usage:
    python bench/run.py [bench/targets.json]

Each target in targets.json runs ./scan (or ./assess), the resulting findings.json
is scored against its ground truth (bench/score.score), and a Markdown rollup is
written. The seeded app measures false negatives; the known-clean app measures the
false-positive rate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import score as score_mod  # noqa: E402
import coverage as coverage_mod  # noqa: E402


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def _server():
    return os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080/api")


def _run_scan(target: dict) -> dict:
    """Start a scan via the CLI, wait, and return the parsed findings.json."""
    cli = os.path.join(ROOT, "assess" if target.get("mode") == "assess" else "scan")
    cmd = [cli, target["url"], "--authorized", *target.get("args", [])]
    print(f"  $ {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=3600)
    out = proc.stdout + proc.stderr
    # The CLI prints "report: <dir>" on completion; fall back to newest reports/ dir.
    report_dir = ""
    for line in out.splitlines():
        line = line.strip()
        if "report:" in line:
            report_dir = line.split("report:", 1)[1].strip()
    if not report_dir or not os.path.isdir(report_dir):
        reports = os.path.join(ROOT, "reports")
        dirs = [os.path.join(reports, d) for d in os.listdir(reports)
                if os.path.isdir(os.path.join(reports, d))]
        report_dir = max(dirs, key=os.path.getmtime) if dirs else ""
    fpath = os.path.join(report_dir, "findings.json")
    if not os.path.isfile(fpath):
        return {"_error": f"no findings.json (cli output tail: {out[-300:]})"}
    data = _load(fpath)
    tri = data.get("triage") or {}
    return {"findings": tri.get("findings") or [], "report_dir": report_dir}


def _findings_for(target: dict) -> list:
    res = _run_scan(target)
    if "_error" in res:
        print(f"  ! {res['_error']}")
        return []
    return res["findings"]


def main(targets_path: str) -> int:
    cfg = _load(targets_path)
    rows = []
    for t in cfg.get("targets", []):
        print(f"- benchmarking {t['name']} ({t['url']})")
        expected = _load(os.path.join(ROOT, t["expected"]))["expected"] if t.get("expected") else []
        findings = _findings_for(t)
        rows.append({"name": t["name"], "score": score_mod.score(expected, findings)})

    # Header = the offline oracle-readiness report (coverage + adversarial + held-out split),
    # with the live recall/FP table populated from this run (design §19.2; unifies make bench).
    catalog = coverage_mod._load_catalog(os.path.join(ROOT, "catalog", "objectives.yaml"))
    positives, negatives = coverage_mod.load_fixtures(os.path.join(HERE, "expected"))
    k = int((cfg.get("holdout") or {}).get("k") or 2)
    import datetime
    as_of = "live run " + datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    md = coverage_mod.build_report(catalog, positives, negatives, cfg.get("targets", []),
                                   k=k, as_of=as_of, live_rows=rows)

    # Per-class recall detail (ROADMAP E10): a class isn't "covered" until detected on a live scan.
    lines = ["## Per-class recall (live)", "", "| Target | Class | Detected | Total | Recall |",
             "|--------|-------|----------|-------|--------|"]
    for r in rows:
        for cls, slot in sorted((r["score"].get("by_class") or {}).items()):
            lines.append(f"| {r['name']} | {cls} | {slot['detected']} | {slot['total']} | {slot['recall']} |")
    out_path = os.path.join(ROOT, "reports", "BENCH.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        fh.write(md + "\n".join(lines) + "\n")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "targets.json")))
