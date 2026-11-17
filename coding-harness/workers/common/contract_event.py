"""Frozen-contract change as a controlled event (roadmap H4, closure plan WP1.3) — pure decisions.

A fork that edits a frozen contract path is never merged ambiently and never retried blindly. The
integrator blocks the proposer with ``contract_change_pending``, posts a ``contract_change`` escalation
and records the Lead's decision at once: reject — a frozen contract stays frozen, and humans are
passive observers who never approve. The **next standup** applies it (re-dispatches the proposer
against the contract as-is). Repeated edits past ``maxContractEvents`` block the item as churn.

This module is the pure half: which recorded decisions are still unapplied and the current contract
revision.
"""

from __future__ import annotations

CONTRACT_KIND = "contract"            # {paths, frozen, revision, approvedBy, head}
CONTRACT_EVENT_KIND = "contract_event"  # {item, decision, applied: true} — one per applied resolution
CHANGE_EXCEPTION = "contract_change"
CHURN_EXCEPTION = "contract_churn"
PENDING_REASON = "contract_change_pending"


def _mapping(v) -> dict:
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else {}


def _data(fact) -> dict:
    fact = _mapping(fact)
    return _mapping(fact.get("data")) if "data" in fact else fact


def current_contract(contract_facts: list) -> dict:
    """The latest contract fact wins: ``{paths, revision}`` (revision 1 = the plan-time freeze)."""
    paths: list[str] = []
    revision = 0
    for fact in contract_facts or []:
        d = _data(fact)
        paths = [str(p) for p in (d.get("paths") or []) if str(p).strip()] or paths
        revision = int(d.get("revision") or (revision + 1))
    return {"paths": paths, "revision": max(revision, 1 if paths else 0)}


def change_count(exceptions: list, item_id: str) -> int:
    """How many contract-change proposals this item has already raised (open or resolved)."""
    return sum(1 for e in exceptions or []
               if str(_data(e).get("kind")) == CHANGE_EXCEPTION
               and str(_data(e).get("item")) == str(item_id)
               and not _data(e).get("resolved"))


def pending_decisions(exceptions: list, events: list) -> list[dict]:
    """Resolved ``contract_change`` exceptions not yet applied by a standup, oldest first.

    Matching is by count per item: the k-th resolution of an item's contract change is applied by the
    k-th ``contract_event`` for that item. A resolution without an ``approve``/``reject`` decision is a
    plain dismissal (``reject``)."""
    resolutions: dict[str, list[dict]] = {}
    for e in exceptions or []:
        d = _data(e)
        if str(d.get("kind")) != CHANGE_EXCEPTION or not d.get("resolved"):
            continue
        decision = str(d.get("decision") or "").strip().lower()
        resolutions.setdefault(str(d.get("item")), []).append({
            "item": str(d.get("item")),
            "decision": decision if decision in ("approve", "reject") else "reject",
            "text": str(d.get("detail") or ""),
            "by": str(d.get("by") or "human"),
        })
    applied: dict[str, int] = {}
    for ev in events or []:
        d = _data(ev)
        applied[str(d.get("item"))] = applied.get(str(d.get("item")), 0) + 1
    out = []
    for item, recs in resolutions.items():
        out.extend(recs[applied.get(item, 0):])
    return out
