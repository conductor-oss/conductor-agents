#!/usr/bin/env python3
"""I7 runtime validator for Exploit Deepening (docs/EXPLOIT_DEEPENING_VERIFICATION.md).

Asserts, against a real run's dossier.json, the invariants that can only be checked on produced
artifacts:

  I4 (well-formedness)   every operation-ledger `family` is in the known token set (∪ {other}).
  I7 (no double-count)   the reported `feature_exercise.technique_coverage` equals what is
                         recomputed from the operation ledger — the report does not inflate the
                         ledger. NOTE: |ledger| is a *lower bound* on attempts (the `family` kwarg
                         is optional/agent-discretion), so this checks faithfulness-to-ledger, NOT
                         attempt-count ground truth (see I4/I7 in the verification doc).
  I7 (volume, advisory)  reports recorded operation counts; the hard global rate/volume budget is
                         enforced at runtime by halt.py and is not re-derivable here.

Hard failures (I4, no-double-count) exit non-zero. A run with no recorded technique families
(e.g. a pre-feature run) passes vacuously. Usage:
    python bench/verify_deepening.py reports/<run-id>/dossier.json
    python bench/verify_deepening.py --selftest
"""
from __future__ import annotations

import json
import sys

FAMILY_TOKENS = {
    "reflection-breakout", "alternate-engine", "encoding-bypass", "gadget-chain", "oob-exfil",
    "syntactic", "channel-variant", "timing", "chain", "other",
}


def _coverage_from_ledger(ledger: list) -> dict:
    """Mirror of feature_exercise.technique_coverage — recomputed independently here so the check
    is a genuine cross-validation of the dossier's reported coverage, not a tautology."""
    by: dict = {}
    for op in ledger or []:
        if not isinstance(op, dict):
            continue
        fam = op.get("family")
        if not fam:
            continue
        oid = str(op.get("objective_id") or "other")
        by.setdefault(oid, set()).add(fam)
    return {oid: {"tried_families": sorted(f), "n_tried": len(f)} for oid, f in by.items()}


def verify(dossier: dict) -> tuple[bool, list]:
    ledger = dossier.get("operation_ledger") or []
    fe = dossier.get("feature_exercise") or {}
    reported = fe.get("technique_coverage") or {}
    family_ops = [o for o in ledger if isinstance(o, dict) and o.get("family")]
    errors, notes = [], []

    if not family_ops:
        notes.append("no recorded technique families in this run (vacuously OK — feature not exercised)")

    # I4 — well-formedness
    bad = sorted({o.get("family") for o in family_ops if o.get("family") not in FAMILY_TOKENS})
    if bad:
        errors.append(f"I4: out-of-vocabulary family tokens in ledger: {bad}")

    # I7 — no double-count: reported coverage must equal the ledger-recomputed coverage
    recomputed = _coverage_from_ledger(ledger)
    if reported != recomputed:
        errors.append(f"I7: reported technique_coverage != ledger-recomputed.\n  reported={reported}\n  ledger ={recomputed}")

    # I7 — volume (advisory)
    notes.append(f"recorded family attempts: {len(family_ops)} across {len(recomputed)} objective(s) "
                 f"— LOWER BOUND (family kwarg optional); per-objective: "
                 f"{ {k: v['n_tried'] for k, v in recomputed.items()} }")
    return (not errors), errors + ["(note) " + n for n in notes]


def _selftest() -> int:
    ledger = [
        {"type": "injection_attempt", "family": "reflection-breakout", "objective_id": "INFRA-RCE-INJECTION"},
        {"type": "injection_attempt", "family": "alternate-engine", "objective_id": "INFRA-RCE-INJECTION"},
        {"type": "http_request", "method": "GET", "path": "/"},
    ]
    good = {"operation_ledger": ledger,
            "feature_exercise": {"technique_coverage": _coverage_from_ledger(ledger)}}
    ok, msgs = verify(good)
    assert ok, ("faithful dossier should pass", msgs)
    bad_inflated = {"operation_ledger": ledger,
                    "feature_exercise": {"technique_coverage": {"INFRA-RCE-INJECTION": {"tried_families": ["a", "b", "c"], "n_tried": 3}}}}
    ok2, _ = verify(bad_inflated)
    assert not ok2, "inflated coverage must fail I7"
    bad_token = {"operation_ledger": [{"type": "injection_attempt", "family": "made-up", "objective_id": "X"}],
                 "feature_exercise": {"technique_coverage": _coverage_from_ledger([{"family": "made-up", "objective_id": "X"}])}}
    ok3, _ = verify(bad_token)
    assert not ok3, "out-of-vocab family must fail I4"
    print("selftest OK — validator passes a faithful dossier, fails inflation (I7) and bad tokens (I4)")
    return 0


def main(argv: list) -> int:
    if not argv or argv[0] == "--selftest":
        return _selftest()
    with open(argv[0]) as fh:
        dossier = json.load(fh)
    ok, msgs = verify(dossier)
    print(f"=== I7 deepening validation: {argv[0]} ===")
    for m in msgs:
        print(" -", m)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
