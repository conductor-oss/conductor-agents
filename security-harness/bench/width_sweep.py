#!/usr/bin/env python3
"""Width-yield sweep: run the swarm at several wave widths against one target and plot
confirmed-findings-per-cost, to answer "is wider actually better?" (design/SWARM.md §7-8).

Runs each width as a full ./swarm campaign (sequential — they share the fleet/server), reads the
confirmed count + request spend from each campaign's rollup, and feeds the results to
bench/swarm_eval.py for the width-yield curve. Default target is the local Juice Shop (make up),
so the sweep is repeatable and needs no external auth.

Usage:
  python3 bench/width_sweep.py [--target URL] [--widths 8,16,24,48,96] [--rounds 4] [--budget 8000]
Writes reports/width_sweep.md and prints the curve.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # bench/ for swarm_eval
import swarm_eval  # noqa: E402

CONDUCTOR = os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080/api").rstrip("/")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Rough $ proxy per live request (relative comparison is what matters for the curve SHAPE).
COST_PER_REQUEST = float(os.environ.get("SC_COST_PER_REQUEST", "0.01"))


def _api(path):
    try:
        with urllib.request.urlopen(CONDUCTOR + path, timeout=20) as r:
            return json.load(r)
    except Exception:
        return None


def run_one(target: str, width: int, rounds: int, budget: int, capability: int = 3) -> dict:
    """Start one campaign at a given width, wait for it to finish, return {width, confirmed, cost}.

    Robust against the launcher's own lifetime: ./swarm's output goes to a FILE (never a captured pipe,
    which would deadlock on a daemon child's inherited fd), we poll that file for the campaign id, then
    poll the SERVER by API for completion — independent of whether ./swarm has exited yet.

    capability defaults to 3: code_exec needs level 2, and sensitive proofs (secret reads, sensitive
    writes) need level 3; a cap-1 (read-only) sweep confirms NOTHING. Level 4 adds nothing via the
    number (destructive/availability is the separate allowed_classes=["resilience"] opt-in)."""
    argv = ["./swarm", target, "--authorized", "--capability", str(capability),
            "--width", str(width), "--rounds", str(rounds), "--budget", str(budget), "--no-wait"]
    print(f"\n=== width {width}: {' '.join(argv)} ===", flush=True)
    logf = tempfile.NamedTemporaryFile("w+", prefix=f"sweep_w{width}_", suffix=".log", delete=False)
    proc = subprocess.Popen(argv, cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    cid, t0 = None, time.time()
    while time.time() - t0 < 1800:  # bootstrap (stale-fleet reload + register) can take a while
        m = _UUID.findall(open(logf.name).read())
        if m:
            cid = m[-1]
            break
        if proc.poll() is not None:  # launcher exited without printing a campaign id -> real failure
            break
        time.sleep(3)
    if not cid:
        print("  could not parse campaign id:\n" + open(logf.name).read()[-600:], flush=True)
        _reap(proc)
        return {"width": width, "confirmed": 0, "cost": 0.0, "error": "no campaign id"}
    print(f"  campaign {cid} - polling server...", flush=True)
    t0 = time.time()
    try:
        while time.time() - t0 < 6 * 3600:  # no artificial cap on the campaign itself
            w = _api(f"/workflow/{cid}?includeTasks=false") or {}
            st = w.get("status")
            if st in ("COMPLETED", "FAILED", "TERMINATED", "TIMED_OUT"):
                o = w.get("output") or {}
                roll = o.get("rollup") or {}
                confirmed = roll.get("confirmed_count")
                if confirmed is None:
                    confirmed = len(o.get("confirmed_findings") or [])
                requests = (roll.get("spend") or {}).get("requests") or 0
                rounds_run = o.get("rounds_run")
                print(f"  {st}: confirmed={confirmed} requests={requests} rounds={rounds_run}", flush=True)
                return {"width": width, "confirmed": int(confirmed or 0),
                        "cost": round(float(requests) * COST_PER_REQUEST, 4), "cid": cid,
                        "requests": requests, "rounds_run": rounds_run, "status": st}
            time.sleep(20)
        return {"width": width, "confirmed": 0, "cost": 0.0, "error": "timeout", "cid": cid}
    finally:
        _reap(proc)  # the launcher should have exited on --no-wait; clean up if it lingered


def _reap(proc) -> None:
    """Terminate the ./swarm launcher if it is still alive (the nohup'd fleet it started survives)."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="http://localhost:3001")
    ap.add_argument("--widths", default="8,16,24,48,96")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--budget", type=int, default=8000)
    ap.add_argument("--capability", type=int, default=3,
                    help="capability ceiling (default 3 = active + sensitive proofs; 1 = read-only confirms nothing; 4 is a no-op vs 3 unless allowed_classes=resilience)")
    a = ap.parse_args()
    widths = [int(x) for x in a.widths.split(",") if x.strip()]
    runs = [run_one(a.target, w, a.rounds, a.budget, a.capability) for w in widths]
    curve = swarm_eval.width_yield_curve([r for r in runs if "error" not in r])
    best = max(curve, key=lambda p: swarm_eval.confirmed_per_dollar(p["confirmed"], p["cost"])) if curve else None

    lines = ["# Swarm width-yield sweep", "",
             f"- Target: {a.target}", f"- Rounds: {a.rounds} · Budget: {a.budget} · Cost/request: ${COST_PER_REQUEST}", "",
             "| Width | Confirmed | Requests | Cost ($) | Confirmed/$ | Rounds | Status |",
             "|---|---|---|---|---|---|---|"]
    for r in runs:
        cpd = swarm_eval.confirmed_per_dollar(r.get("confirmed", 0), r.get("cost", 0.0))
        lines.append(f"| {r['width']} | {r.get('confirmed',0)} | {r.get('requests','-')} | "
                     f"{r.get('cost',0)} | {cpd:.2f} | {r.get('rounds_run','-')} | {r.get('status', r.get('error','-'))} |")
    if best:
        lines += ["", f"**Best confirmed-per-dollar: width {best['width']}** "
                      f"({swarm_eval.confirmed_per_dollar(best['confirmed'], best['cost']):.2f}/$)."]
    md = "\n".join(lines)
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    with open(os.path.join(ROOT, "reports", "width_sweep.md"), "w") as fh:
        fh.write(md + "\n")
    print("\n" + md + "\n\nsaved -> reports/width_sweep.md")


if __name__ == "__main__":
    main()
