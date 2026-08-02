"""Evidence-state ladder (PLAN_V3 section 5.4).

A finding's severity says how bad it is IF real; it says nothing about how strongly we established
that it is real *against the live target*. Rendering a static source signal ("default secret is in
the repo") at the same rating as a runtime-confirmed exploit ("we forged a token and read another
tenant's data") overstates live risk. This module classifies every reported finding onto a ladder
of how strongly its impact was established, weakest -> strongest:

  source_only            - static/source/dependency signal; no runtime attempt reached it.
  source_attested        - a source signal whose reachability was attested live (e.g. a DAST tool
                           observed it at runtime), but impact was not exploited to a proof.
  runtime_reproduced     - actively exploited by the agent and re-verified in-band (deep_exploit).
  runtime_oob_confirmed  - confirmed by an independent out-of-band callback receipt (strongest).

Only the runtime_* rungs drive the LIVE risk rating; the source_* rungs drive a separate
"source-candidate" rating. The live rating is derived from the runtime-confirmed set directly
(authoritative on `source_tool`), never from a title join, so the headline cannot be inflated by
how the triage LLM happened to word a finding. Per-finding tags on the rendered (triage) findings
use a conservative best-effort title match; a mismatch can only *under*-tag a rendered row, never
raise the live rating. Pure logic, unit-tested (`tests/test_evidence_state.py`).
"""

from __future__ import annotations

import re

SOURCE_ONLY = "source_only"
SOURCE_ATTESTED = "source_attested"
RUNTIME_REPRODUCED = "runtime_reproduced"
RUNTIME_OOB_CONFIRMED = "runtime_oob_confirmed"
LADDER = (SOURCE_ONLY, SOURCE_ATTESTED, RUNTIME_REPRODUCED, RUNTIME_OOB_CONFIRMED)
RUNTIME_RUNGS = (RUNTIME_REPRODUCED, RUNTIME_OOB_CONFIRMED)

_SEV_ORDER = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
NONE_RATING = "None"


def _tokens(title) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", str(title or "").lower()) if len(w) >= 3}


def _match(a: set[str], b: set[str]) -> bool:
    """Conservative title match: high Jaccard for short titles, or >=3 shared significant tokens
    for long ones. Deliberately strict to avoid tagging a source finding as runtime."""
    if not a or not b:
        return False
    inter = len(a & b)
    if inter == 0:
        return False
    return (inter / len(a | b)) >= 0.5 or inter >= 3


def _any_match(ftok: set[str], cands: list[set[str]]) -> bool:
    return any(_match(ftok, c) for c in cands)


def _max_sev(findings: list) -> str:
    best, label = 0, NONE_RATING
    for f in findings:
        s = _SEV_ORDER.get(str((f or {}).get("severity")).lower(), 0)
        if s > best:
            best, label = s, str(f.get("severity"))
    return label


def is_oob(f: dict) -> bool:
    return (f.get("source_tool") == "oob_confirmed" or f.get("oob_confirmed") is True
            or f.get("evidence_state") == RUNTIME_OOB_CONFIRMED)


def is_runtime(f: dict) -> bool:
    """A finding produced/promoted by the live exploit+verify path (as opposed to a scanner)."""
    return (f.get("source_tool") in ("deep_exploit", "oob_confirmed")
            or f.get("confirmed") is True or f.get("oob_confirmed") is True
            or f.get("evidence_state") in RUNTIME_RUNGS)


def annotate(triage: dict, confirmed: list | None = None, surface: list | None = None) -> dict:
    """Stamp `evidence_state` on each non-false-positive triage finding and split the risk rating.

    Returns {triage (findings stamped), live_risk_rating, source_candidate_rating,
    evidence_state_counts, confirmed_count, oob_confirmed_count}. `live_risk_rating` is the max
    severity of the runtime-confirmed set (``None`` when nothing was confirmed live);
    `source_candidate_rating` is the max severity of the rendered findings that are NOT runtime.
    """
    triage = dict(triage or {})
    confirmed = [f for f in (confirmed or []) if isinstance(f, dict)]
    surface = [f for f in (surface or []) if isinstance(f, dict)]

    oob_conf = [f for f in confirmed if is_oob(f)]
    repro_conf = [f for f in confirmed if is_runtime(f) and not is_oob(f)]
    # The live rating comes straight from the confirmed set (any confirmed finding is live-proven).
    live_rating = _max_sev(confirmed)

    oob_tok = [_tokens(f.get("title")) for f in oob_conf]
    repro_tok = [_tokens(f.get("title")) for f in repro_conf]
    observed_tok = [_tokens(f.get("title")) for f in surface
                    if str(f.get("provenance")) == "observed"]

    out: list = []
    source_bucket: list = []
    counts = {r: 0 for r in LADDER}
    for raw in (triage.get("findings") or []):
        f = dict(raw) if isinstance(raw, dict) else {}
        if f.get("false_positive") is True:
            out.append(f)
            continue
        ftok = _tokens(f.get("title"))
        if _any_match(ftok, oob_tok):
            rung = RUNTIME_OOB_CONFIRMED
        elif _any_match(ftok, repro_tok):
            rung = RUNTIME_REPRODUCED
        elif _any_match(ftok, observed_tok):
            rung = SOURCE_ATTESTED
        else:
            rung = SOURCE_ONLY
        f["evidence_state"] = rung
        counts[rung] += 1
        if rung not in RUNTIME_RUNGS:
            source_bucket.append(f)
        out.append(f)

    triage["findings"] = out
    return {
        "triage": triage,
        "live_risk_rating": live_rating,
        "source_candidate_rating": _max_sev(source_bucket),
        "evidence_state_counts": counts,
        "confirmed_count": len(confirmed),
        "oob_confirmed_count": len(oob_conf),
    }
