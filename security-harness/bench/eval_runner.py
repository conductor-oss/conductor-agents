#!/usr/bin/env python3
"""The §19.2 paired EVAL seam — score a config (champion or challenger) on the held-out split.

This is the *live* half of the write-back cycle: it actually runs the harness against a target
and scores the findings against ground truth. ``make_eval_fn`` returns an ``eval_fn(overlay,
target) -> {recall, fp_rate, per_class, cost}`` that `hc_writeback.run_cycle` calls for the
champion baseline (overlay=None) and each challenger (overlay={surface,path,content}).

A challenger is evaluated by overlaying its edited content onto the surface file, running the
scan, scoring, and ALWAYS restoring the original file (try/finally). IMPORTANT: prompts are
injected into taskdefs at register time, so for a *prompt* overlay to actually reach the running
workers the caller must re-register between write and scan — pass an ``on_apply`` hook (e.g. a
``make register`` shim) for a faithful measurement; without it, a prompt overlay is written to
disk but the live workers keep the prior prompt (the measurement would be a no-op and the gate
would correctly find "not significant").

Needs a running Conductor server + workers + reachable targets (it shells out to ./scan via
run.py). The loop logic itself is unit-tested with a stub eval_fn; this module is the live wiring.
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import run as bench_run  # noqa: E402  (_run_scan / _load)
import score as score_mod  # noqa: E402


def _per_class_recall(by_class: dict) -> dict:
    """score.by_class is {cls: {detected,total,recall}}; protected_ok wants {cls: recall}."""
    return {cls: slot.get("recall", 0.0) for cls, slot in (by_class or {}).items()}


def make_eval_fn(expected_by_target: dict, *, root: str = ROOT, on_apply=None):
    """Build the live eval_fn. ``expected_by_target`` maps a target name to its expected-weakness
    list (the ground truth). ``on_apply(path)`` is an optional hook run after writing an overlay
    (and again after restoring) so a prompt change reaches the workers (e.g. re-register)."""

    def eval_fn(overlay: dict | None, target: dict) -> dict | None:
        expected = expected_by_target.get(target.get("name")) or []
        path = (overlay or {}).get("path")
        original = None
        try:
            if overlay and path:
                with open(path, encoding="utf-8") as fh:
                    original = fh.read()
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(overlay.get("content") or "")
                if on_apply:
                    on_apply(path)
            t0 = time.monotonic()
            findings = bench_run._findings_for(target)
            cost = round(time.monotonic() - t0, 2)
            sc = score_mod.score(expected, findings)
            return {"recall": sc.get("recall", 0.0), "fp_rate": sc.get("fp_rate", 0.0),
                    "per_class": _per_class_recall(sc.get("by_class")), "cost": cost}
        except Exception as exc:                       # a broken scan must not crash the cycle
            print(f"  ! eval_fn failed for {target.get('name')}: {exc}")
            return None
        finally:
            if original is not None and path:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(original)
                if on_apply:
                    on_apply(path)

    return eval_fn
