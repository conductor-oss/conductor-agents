#!/usr/bin/env python3
"""Regenerate a finished campaign's report PDF WITH the verbose reproduction section, offline.

Future runs get the verbose reproduction automatically (scribe_rollup + compose_report build it into
report.md -> PDF). This tool back-fills the SAME content onto an already-finished run: it rebuilds the
"## Reproduction & Validation" section with `common.repro` (identical to the live pipeline — the once
`preamble()` + a per-finding `markdown()` block), merges it onto the run's report.md, sanitizes it the
way the pipeline does (`common.report_gate` — the GENERATE_PDF font can't render some glyphs), and
renders via the server-side `render_pdf` GENERATE_PDF workflow (no local md->pdf tool exists).

Usage:
  python3 bench/repro_report.py --cid <campaign-id> [--render]
Writes reports/<cid>/reproduction.md, and with --render reports/<cid>/report_with_reproduction.pdf.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "workers"))
from common import repro as repro_mod  # noqa: E402  (read-only reuse of the live pipeline's renderer)
try:
    from common import report_gate  # noqa: E402
except Exception:
    report_gate = None

CONDUCTOR = os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080/api").rstrip("/")


def _findings(cid: str) -> list[dict]:
    for base in (os.path.join(ROOT, "state"), os.path.join(ROOT, "workers", "state")):
        d = os.path.join(base, "blackboard", "facts", cid, "finding")
        if os.path.isdir(d):
            fs = [json.load(open(f)).get("data", {}) for f in glob.glob(d + "/*.json")]
            fs = [f for f in fs if f.get("confirmed") is True and f.get("refuted") is not True]
            sev = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Info": 4}
            return sorted(fs, key=lambda f: (sev.get(f.get("severity"), 5), f.get("title", "")))
    return []


def _report_dir(cid: str) -> str:
    cands = [os.path.join(ROOT, "reports", cid), os.path.join(ROOT, "workers", "reports", cid)]
    # Prefer the directory that actually holds report.md (older runs wrote under workers/reports).
    for d in cands:
        if os.path.isfile(os.path.join(d, "report.md")):
            return d
    for d in cands:
        if os.path.isdir(d):
            return d
    return cands[0]


def build_section(cid: str) -> tuple[str, int]:
    """The '## Reproduction & Validation' section exactly as the live compose_report builds it."""
    fs = _findings(cid)
    if not fs:
        return "", 0
    parts = ["\n\n---\n\n## Reproduction & Validation\n",
             "Step-by-step reproduction for each confirmed finding so it can be independently "
             "reproduced, validated, fixed, and verified.\n",
             repro_mod.preamble()]
    for i, f in enumerate(fs, 1):
        parts.append(f"### R-{i}. {f.get('title','untitled')}\n")
        parts.append(repro_mod.markdown(f) + "\n")
    return "\n".join(parts), len(fs)


def render_pdf(cid: str, rd: str, section: str):
    report = os.path.join(rd, "report.md")
    if not os.path.isfile(report):
        print("no report.md to merge; wrote reproduction.md only")
        return
    base = open(report).read()
    # Replace an existing Reproduction section if present, else append.
    marker = "## Reproduction & Validation"
    if marker in base:
        base = base[: base.index(marker)].rstrip().rstrip("-").rstrip()
    merged = base.rstrip() + section
    if report_gate:
        merged = report_gate.check(merged, deep=False).get("markdown") or merged
    body = json.dumps({"markdown": merged, "title": f"Swarm Report + Reproduction - {cid}"}).encode()
    wid = urllib.request.urlopen(urllib.request.Request(
        CONDUCTOR + "/workflow/render_pdf", data=body,
        headers={"Content-Type": "application/json"}, method="POST"), timeout=30).read().decode().strip()
    print("render workflow:", wid)
    for _ in range(60):
        w = json.load(urllib.request.urlopen(CONDUCTOR + f"/workflow/{wid}?includeTasks=false", timeout=20))
        st = w.get("status")
        if st == "COMPLETED":
            src = (w.get("output") or {}).get("pdf", "").replace("file://", "")
            dst = os.path.join(rd, "report_with_reproduction.pdf")
            open(dst, "wb").write(open(src, "rb").read())
            print("saved", dst, os.path.getsize(dst), "bytes")
            return
        if st in ("FAILED", "TERMINATED", "TIMED_OUT"):
            print("render failed:", w.get("reasonForIncompletion"))
            return
        time.sleep(3)
    print("render timed out")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cid", required=True)
    ap.add_argument("--render", action="store_true")
    a = ap.parse_args()
    section, n = build_section(a.cid)
    rd = _report_dir(a.cid)
    os.makedirs(rd, exist_ok=True)
    open(os.path.join(rd, "reproduction.md"), "w").write(section.lstrip())
    print(f"{n} findings -> {rd}/reproduction.md ({len(section)} chars)")
    if a.render:
        render_pdf(a.cid, rd, section)


if __name__ == "__main__":
    main()
