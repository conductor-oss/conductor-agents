#!/usr/bin/env python3
"""Distill human-ratified confirmed findings from a real scan into the LIVING benchmark corpus (§19.2).

`oracle.distill()` + `oracle.merge_fixtures()` (the forgetting guard: only adds, dedupes by
signature) have existed and been unit-tested, but were never wired to production runs — so the
benchmark stayed authored-only and the oracle could not GROW from real findings. This CLI closes that
loop: it turns a real confirmed finding into a permanent regression fixture, so HC climbs toward
reproducing real-world findings and any future change that re-breaks one is rejected by the gate.

Ratification is the human gate (H6): a finding is distilled only if it carries ``ratified: true``,
OR the operator passes ``--ratify-all`` (deliberately running this CLI IS the ratification). It is
never called automatically — auto-distilling unratified findings is the overfitting trap the oracle
module warns against.

  python bench/distill_findings.py reports/<scan_id>/dossier.json [--ratify-all] \
      [--into bench/expected/ratified.json]
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "workers"))
import oracle as oracle_mod  # noqa: E402

DEFAULT_INTO = os.path.join(HERE, "expected", "ratified.json")
_DOC = ("Living corpus (design §19.2): fixtures distilled from HUMAN-RATIFIED real-run findings via "
        "oracle.distill (origin:ratified). The forgetting guard only adds + dedupes by signature, so "
        "a past real win keeps being measured and a change that re-breaks it is rejected.")


def distill_findings(confirmed: list, *, ratify_all: bool, into: str, as_of: str) -> dict:
    """Pure core: select ratified findings, distill, merge into the corpus at ``into``. Returns the
    merged corpus dict + counts (does not write — caller persists)."""
    ratified = [f for f in (confirmed or [])
                if isinstance(f, dict) and (ratify_all or f.get("ratified") is True)]
    fixtures = [oracle_mod.distill(f, as_of=as_of) for f in ratified]
    try:
        existing = (json.load(open(into)) or {}).get("expected", [])
    except Exception:
        existing = []
    merged = oracle_mod.merge_fixtures(existing, fixtures)
    return {"corpus": merged["corpus"], "added": merged["added"],
            "ratified": len(ratified), "confirmed": len(confirmed or [])}


def _persist(into: str, corpus: list) -> None:
    os.makedirs(os.path.dirname(into), exist_ok=True)
    with open(into, "w") as fh:
        json.dump({"_doc": _DOC, "expected": corpus}, fh, indent=2)


def distill_ratified_reports(reports_dir: str, *, into: str, as_of: str, ratify_all: bool = False) -> dict:
    """The post-run RATIFICATION HOOK: drain every dossier's HUMAN-RATIFIED confirmed findings
    (``ratified: true``) across ``reports_dir`` into the living corpus, forgetting-guarded by
    ``merge_fixtures`` (only adds, dedupes by signature). Persists ``into`` iff something new was
    added. ``ratify_all`` is the operator's deliberate blanket assertion (H6) — OFF for the automatic
    hook, so unratified findings never auto-distill (the overfitting trap oracle.py warns of)."""
    confirmed = []
    for path in sorted(glob.glob(os.path.join(reports_dir, "*", "dossier.json"))):
        try:
            doc = json.load(open(path))
        except Exception:
            continue
        for f in (doc.get("confirmed_findings") or []):
            if isinstance(f, dict):
                confirmed.append(f)
    res = distill_findings(confirmed, ratify_all=ratify_all, into=into, as_of=as_of)
    res["scanned"] = len(confirmed)
    if res["added"]:
        _persist(into, res["corpus"])
    return res


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Distill ratified findings into the living benchmark corpus.")
    ap.add_argument("report", help="path to a scan dossier.json (or findings-style json with confirmed_findings)")
    ap.add_argument("--ratify-all", action="store_true",
                    help="treat every confirmed finding as human-ratified (the operator asserts H6)")
    ap.add_argument("--into", default=DEFAULT_INTO, help="living corpus file to grow")
    ap.add_argument("--as-of", default="", help="timestamp stamp (default: now)")
    a = ap.parse_args(argv)

    try:
        doc = json.load(open(a.report))
    except Exception as exc:
        print(f"ERROR: cannot read {a.report}: {exc}", file=sys.stderr)
        return 1
    confirmed = doc.get("confirmed_findings") if isinstance(doc, dict) else None
    confirmed = confirmed if isinstance(confirmed, list) else []
    as_of = a.as_of or ("distilled " + datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z")

    res = distill_findings(confirmed, ratify_all=a.ratify_all, into=a.into, as_of=as_of)
    if res["ratified"] == 0:
        print(f"no ratified findings ({res['confirmed']} confirmed). Mark findings ratified:true "
              f"or pass --ratify-all to ratify them all.")
        return 0
    _persist(a.into, res["corpus"])
    print(f"distilled {res['ratified']} ratified finding(s); added {len(res['added'])} new fixture(s) "
          f"to {os.path.relpath(a.into, ROOT)} (corpus now {len(res['corpus'])}).")
    if res["added"]:
        print("  added:", ", ".join(res["added"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
