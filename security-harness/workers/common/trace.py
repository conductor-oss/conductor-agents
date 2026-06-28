"""Structured trace corpus for the §19 hill-climbing loop (§19.2).

The harness already persists findings + coverage + run history; HC additionally needs the
*reasoning* behind each verdict — which evidence bar was checked and why it passed or failed
— in a machine-readable, clusterable form. This module is that record + the clustering that
turns scattered per-run verdicts into *recurring failure signatures* (the corroborated
signal H4 requires before any config change is proposed).

A trace record is data about the harness's own behavior; per H7 it is untrusted input to
the optimizer (it may contain target-influenced strings), so clustering keys are built from
*controlled* fields (objective_id, outcome, evidence_bar) — never from free-text the target
could shape. Pure logic; persistence is a best-effort jsonl append.
"""

from __future__ import annotations

import json
import os

# confirmed/rejected/inconclusive are finding verdicts; blocked = input-gap (honesty guard);
# exhausted = a technique ladder was fully walked without confirmation (attempt-level signal).
OUTCOMES = ("confirmed", "rejected", "inconclusive", "blocked", "exhausted")

# Provenance of a trace, so diagnose() can route attempt-level signal differently from verdicts.
SIGNAL_KINDS = ("verdict", "deepen", "technique_coverage", "triage")


def record(
    *,
    run_id: str,
    objective_id: str,
    outcome: str,
    as_of: str,
    evidence_bar: str = "",
    reason: str = "",
    hypothesis: str = "",
    persona: str = "",
    finding_sig: str = "",
    signal_kind: str = "verdict",
    family: str = "",
    sink_class: str = "",
    n_tried: int = 0,
    exhausted: bool = False,
    triage_class: str = "",
) -> dict:
    """One trace entry. ``outcome`` in OUTCOMES; ``evidence_bar`` names the per-class bar checked;
    ``reason`` is bounded free text (H7-sanitized before any proposer sees it). The remaining fields
    are CONTROLLED enums/bounded values (never target free-text) carrying attempt-level signal:
    ``signal_kind`` (provenance), ``family``/``sink_class`` (deepen technique-ladder vocab),
    ``n_tried``/``exhausted`` (ladder coverage), ``triage_class`` (feature-sweep injection class)."""
    return {
        "run_id": run_id,
        "objective_id": objective_id,
        "outcome": outcome if outcome in OUTCOMES else "inconclusive",
        "evidence_bar": evidence_bar,
        "reason": reason,
        "hypothesis": hypothesis,
        "persona": persona,
        "finding_sig": finding_sig,
        "signal_kind": signal_kind if signal_kind in SIGNAL_KINDS else "verdict",
        "family": family,
        "sink_class": sink_class,
        "n_tried": int(n_tried or 0),
        "exhausted": bool(exhausted),
        "triage_class": triage_class,
        "as_of": as_of,
    }


def from_findings(run_id: str, as_of: str, *, confirmed=None, rejected=None, blind=None) -> list:
    """Build verdict-with-reasons trace records from a run's findings (P3-5): the persisted corpus
    ``hc_analyze`` mines instead of a mirrored one. Outcomes map confirmed/rejected -> their name,
    blind leads -> ``inconclusive``. Clustering keys off the CONTROLLED fields only (objective_id /
    outcome / evidence_bar, see ``_sig``); ``reason`` is bounded free text (H7: sanitized before any
    proposer sees it). One record per finding; the corpus grows across runs (recurrence = signal)."""
    recs = []
    for outcome, items in (("confirmed", confirmed), ("rejected", rejected), ("inconclusive", blind)):
        for f in items or []:
            if not isinstance(f, dict):
                continue
            recs.append(record(
                run_id=run_id,
                objective_id=str(f.get("objective_id") or f.get("objective_class") or "other"),
                outcome=outcome,
                as_of=as_of,
                evidence_bar=str(f.get("class") or f.get("objective_class") or ""),
                reason=str(f.get("evidence") or f.get("reason") or f.get("title") or "")[:200],
                finding_sig=str(f.get("finding_sig") or f.get("content_hash") or f.get("signature") or ""),
            ))
    return recs


# injection class -> the catalog objective it maps to (kept local so this core module stays
# dependency-free; mirrors features.CLASS_OBJECTIVE).
_CLASS_OBJECTIVE = {
    "sqli": "INFRA-RCE-INJECTION", "ssti": "INFRA-RCE-INJECTION", "command": "INFRA-RCE-INJECTION",
    "eval": "INFRA-RCE-INJECTION", "rce": "INFRA-RCE-INJECTION", "xss": "CLIENT-XSS-CSRF",
    "traversal": "INFRA-PATH-TRAVERSAL", "ssrf": "INFRA-SSRF", "open-redirect": "INFRA-SSRF",
}


def from_technique_coverage(run_id: str, as_of: str, technique_coverage: dict | None,
                            confirmed=None) -> list:
    """Attempt-level signal from `feature_exercise.technique_coverage` ({objective: {tried_families,
    n_tried}}). For an objective with NO confirmation: few families tried -> a breadth-gap signal;
    several tried and still nothing -> per-family 'exhausted' (the ladder is too weak). This is the
    cheap path — derived from the already-accumulated family-tagged operation ledger."""
    confirmed_objs = {str(f.get("objective_id") or "") for f in (confirmed or []) if isinstance(f, dict)}
    recs = []
    for obj, cov in (technique_coverage or {}).items():
        if not isinstance(cov, dict) or str(obj) in confirmed_objs:
            continue
        fams = [str(x) for x in (cov.get("tried_families") or [])]
        n = int(cov.get("n_tried") or len(fams) or 0)
        if n <= 1:
            recs.append(record(run_id=run_id, as_of=as_of, objective_id=str(obj),
                               outcome="inconclusive", signal_kind="technique_coverage", n_tried=n,
                               reason=f"only {n} technique family attempted for {obj}; breadth gap"))
        else:
            for fam in fams:
                recs.append(record(run_id=run_id, as_of=as_of, objective_id=str(obj),
                                   outcome="exhausted", signal_kind="deepen", family=fam, n_tried=n,
                                   exhausted=True,
                                   reason=f"{n} families tried for {obj}, none confirmed"))
    return recs


def from_deepen_states(run_id: str, as_of: str, deepen_states=None) -> list:
    """Rich attempt-level signal from terminal exploit_deepen states (the full path: carries the
    per-family LESSON of what blocked it). One record per (objective, family); ``exhausted`` marks a
    fully-walked ladder. Tolerant of missing fields. Confirmed states are skipped (not a failure)."""
    recs = []
    for st in (deepen_states or []):
        if not isinstance(st, dict) or st.get("confirmed"):
            continue
        obj = str(st.get("objective_id") or "other")
        sink = str(st.get("sink_class") or "")
        ledger = st.get("ledger") or {}
        tried = sum(1 for s in ledger.values() if isinstance(s, dict) and (s.get("tries") or 0) > 0)
        # exhausted = explicitly flagged, OR several families were walked and still nothing confirmed
        ex = bool(st.get("exhausted")) or tried >= 2
        for fam, slot in (st.get("ledger") or {}).items():
            if not isinstance(slot, dict):
                continue
            recs.append(record(run_id=run_id, as_of=as_of, objective_id=obj,
                               outcome=("exhausted" if ex else "inconclusive"),
                               signal_kind="deepen", family=str(fam), sink_class=sink,
                               n_tried=int(slot.get("tries") or 0), exhausted=ex,
                               reason=str(slot.get("lesson") or "")[:200]))
    return recs


def from_triage(run_id: str, as_of: str, triage_signals=None, confirmed=None) -> list:
    """Feature-sweep signal: a triage canary flagged an injection class on a feature but no finding
    of that class was confirmed -> a classifier/ladder gap to learn from (the 'add the SQLite error
    signature' kind of fix). Deduped per (feature, class)."""
    confirmed_keys = set()
    for f in (confirmed or []):
        if isinstance(f, dict):
            confirmed_keys.add(str(f.get("category") or "").lower())
            confirmed_keys.add(str(f.get("objective_id") or ""))
    recs, seen = [], set()
    for s in (triage_signals or []):
        if not isinstance(s, dict):
            continue
        cls = str(s.get("class") or "").lower()
        obj = _CLASS_OBJECTIVE.get(cls, "other")
        if not cls or cls in confirmed_keys or obj in confirmed_keys:
            continue
        key = (str(s.get("feature_id") or ""), cls)
        if key in seen:
            continue
        seen.add(key)
        recs.append(record(run_id=run_id, as_of=as_of, objective_id=obj, outcome="inconclusive",
                           signal_kind="triage", triage_class=cls,
                           reason=f"triage flagged {cls} on a feature but exploitation never confirmed it"))
    return recs


def append(store: list, rec: dict) -> dict:
    store.append(rec)
    return rec


def persist(path: str, rec: dict) -> None:
    """Best-effort jsonl append (never raises — a trace failure must not crash a run)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def load(path: str) -> list:
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                if line.strip():
                    out.append(json.loads(line))
    except (OSError, ValueError):
        pass
    return out


def failures(store: list) -> list:
    """Traces that did NOT confirm — the signal HC mines (rejected + inconclusive + exhausted;
    'blocked' is an input gap surfaced to the operator, never tuned, so it's excluded)."""
    return [r for r in store if r.get("outcome") in ("rejected", "inconclusive", "exhausted")]


def _sig(rec: dict) -> str:
    # Controlled fields ONLY (H7): objective + outcome + evidence_bar + the attempt-level enums
    # (signal_kind / family / sink_class). NEVER free text (reason/lesson stay out of the key).
    return (f"{rec.get('objective_id', '?')}|{rec.get('outcome', '?')}|{rec.get('evidence_bar', '')}"
            f"|{rec.get('signal_kind', 'verdict')}|{rec.get('family', '')}|{rec.get('sink_class', '')}")


def cluster(store: list) -> list:
    """Group traces by their controlled failure signature, most-recurring first. Each cluster
    carries up to 3 example reasons + the controlled attempt-level fields so diagnose() can route."""
    by: dict = {}
    for r in store or []:
        sig = _sig(r)
        c = by.setdefault(sig, {"signature": sig, "objective_id": r.get("objective_id"),
                                "outcome": r.get("outcome"), "evidence_bar": r.get("evidence_bar"),
                                "signal_kind": r.get("signal_kind", "verdict"), "family": r.get("family", ""),
                                "sink_class": r.get("sink_class", ""), "triage_class": r.get("triage_class", ""),
                                "n_tried": 0, "count": 0, "examples": []})
        c["count"] += 1
        c["n_tried"] = max(c["n_tried"], int(r.get("n_tried") or 0))
        if r.get("reason") and len(c["examples"]) < 3:
            c["examples"].append(r["reason"])
    return sorted(by.values(), key=lambda c: -c["count"])


def recurring(store: list, min_count: int = 2) -> list:
    """Clusters seen at least ``min_count`` times — the corroborated signal (H4): a single
    trace's 'failure' may be LLM noise, so HC acts only on signatures that recur."""
    return [c for c in cluster(store) if c["count"] >= min_count]
