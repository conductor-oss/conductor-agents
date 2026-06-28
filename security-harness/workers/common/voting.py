"""Multi-agent majority voting for the UNCONFIRMABLE finding tail (borrowed from Visa VVAH).

Our verifier is stronger than voting *when a bug is dynamically confirmable*: it re-runs the PoC
and confirms blind bugs out-of-band (an OOB inbound hit is decisive). But a tail of findings can
NEVER be dynamically confirmed in-run — SAST source leads, and blind vectors with no OOB
collaborator. A single skeptic's verdict on those is noisy. VVAH's answer is cheap and sound: run
K independent skeptics and keep only the majority survivors. This module is that aggregation —
pure, so the K skeptic *verdicts* are produced by the workflow (LLM_CHAT_COMPLETE fan-out) and
fed here for a deterministic, refute-by-default tally.

Taxonomy (the `verdict` a finding carries downstream):
  confirmed   — dynamically proven (PoC re-run / OOB hit). Voting never touches these.
  voted       — unconfirmable, but a strict majority of independent skeptics judged it real.
  voted_out   — unconfirmable and it failed the vote (default for ties / no votes — conservative).
"""

from __future__ import annotations

CONFIRMED = "confirmed"
VOTED = "voted"
VOTED_OUT = "voted_out"


def is_dynamically_confirmed(finding: dict) -> bool:
    """True iff the finding was proven by execution (re-run PoC) or out-of-band (canary hit) —
    the cases where voting is unnecessary and would only weaken a hard proof."""
    if finding.get("oob_confirmed") is True or finding.get("confirmed") is True:
        return True
    blob = (str(finding.get("validation") or "") + " " + str(finding.get("status") or "")
            + " " + str(finding.get("lifecycle") or "")).lower()
    if "not confirmed" in blob or "unconfirmed" in blob:
        return False
    return "confirmed" in blob or "verified" in blob or "out-of-band" in blob or "reproduced" in blob


def partition(findings: list) -> dict:
    """Split into {confirmed, unconfirmable}. Only the unconfirmable tail is put to a vote."""
    confirmed, unconfirmable = [], []
    for f in findings or []:
        (confirmed if is_dynamically_confirmed(f) else unconfirmable).append(f)
    return {"confirmed": confirmed, "unconfirmable": unconfirmable}


def _is_real(verdict: dict) -> bool:
    """One skeptic's vote -> does it judge the finding REAL? Refute-by-default: an explicit
    ``refuted: true`` or ``real: false`` is a no; anything ambiguous counts as a no (the verifier
    ethos is 'refute unless proven'). ``real: true`` / ``refuted: false`` is a yes."""
    if not isinstance(verdict, dict):
        return False
    if "real" in verdict:
        return bool(verdict["real"])
    if "refuted" in verdict:
        return not bool(verdict["refuted"])
    return False


def majority(verdicts: list) -> dict:
    """Tally K skeptic verdicts. Survives on a STRICT majority of REAL votes (ties do NOT survive
    — conservative, matching refute-by-default). Returns {survives, real, total}."""
    verdicts = [v for v in (verdicts or []) if isinstance(v, dict)]
    total = len(verdicts)
    real = sum(1 for v in verdicts if _is_real(v))
    return {"survives": (total > 0 and real * 2 > total), "real": real, "total": total}


def label(finding: dict, verdicts: list) -> dict:
    """Return a copy of an UNCONFIRMABLE finding with its vote verdict attached. Confirmed findings
    should not be passed here (they keep ``confirmed``); ``partition`` separates them first."""
    tally = majority(verdicts)
    out = dict(finding)
    out["verdict"] = VOTED if tally["survives"] else VOTED_OUT
    out["vote_summary"] = tally
    return out


def apply(findings: list, votes_by_key: dict, *, key: str = "id") -> list:
    """Label a finding set end-to-end: dynamically-confirmed findings get ``verdict=confirmed``;
    each unconfirmable finding is voted using ``votes_by_key[finding[key]]`` (a list of skeptic
    verdicts; missing -> no votes -> voted_out). Order preserved."""
    votes_by_key = votes_by_key or {}
    out = []
    for f in findings or []:
        if is_dynamically_confirmed(f):
            out.append({**f, "verdict": CONFIRMED})
        else:
            out.append(label(f, votes_by_key.get(f.get(key))))
    return out


def survivors(findings: list, votes_by_key: dict, *, key: str = "id") -> list:
    """The reportable set after voting: confirmed + voted (drops voted_out unconfirmable noise)."""
    return [f for f in apply(findings, votes_by_key, key=key) if f.get("verdict") in (CONFIRMED, VOTED)]
