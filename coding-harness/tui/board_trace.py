"""Audit trace of one work item (roadmap §7 open question #6, closure plan WP10).

A nondeterministic swarm must still be explainable to the precise-failure-mechanism bar (AGENTS.md):
*which agent, given which lessons and directives, at which base commit and contract revision, produced
which change, and what happened to it*. Every one of those is already an append-only fact on the board;
this folds them into one ordered timeline for an item. Pure read — a view over the boards (§4.8).
"""

from __future__ import annotations

import sys
from pathlib import Path

_WORKERS = Path(__file__).resolve().parents[1] / "workers"
if str(_WORKERS) not in sys.path:
    sys.path.insert(0, str(_WORKERS))

from common import blackboard as bb  # noqa: E402

_KINDS = ("item", "state", "dispatch", "session", "delivery", "review", "directive", "lesson",
          "exception", "contract_proposal", "contract_event", "contract", "spend", "swarm_report")


def _about(kind: str, author: str, data: dict, item: str) -> bool:
    if kind in ("item",):
        return str(data.get("id")) == item
    if kind == "directive":
        return str(data.get("target")) in (item, "team", f"agent:{item}") or data.get("op") == "lift"
    if kind == "lesson":
        return item in str(data.get("provenance") or "") or author.endswith(item) or author == item
    if kind == "spend":
        a = str(data.get("author") or author)
        return a in (f"implementer:{item}", f"review_{item}", f"acceptance:{item}") or a.startswith(f"implementer:{item}_c")
    if kind == "contract":
        return str(data.get("proposer") or "") == item
    return str(data.get("item")) == item


def _summary(kind: str, d: dict) -> str:
    if kind == "state":
        return f"{d.get('state')}" + (f" — {d.get('reason')}" if d.get("reason") else "") + f" (by {d.get('by')})"
    if kind == "dispatch":
        # Say only what the record holds: a swarm item's start names its round, rungs and cap; the team
        # pipeline's names its candidate, base, contract revision and prompt. Fields a record lacks were
        # printed as "None" ("dispatched None attempt None on None ...", run 649fa3c8).
        if d.get("candidate") is None and d.get("round") is not None:
            return (f"started in round {d.get('round')} on rung {d.get('rung')} ({d.get('rungReason') or 'no reason given'})"
                    + (f"; its reviewer on rung {d.get('reviewRung')} ({d.get('reviewRungReason')})"
                       if d.get("reviewRung") is not None else "")
                    + (f"; spend cap ${float(d['capUsd']):.2f} per session" if d.get("capUsd") is not None else ""))
        parts = [f"dispatched {d.get('candidate') or d.get('item') or ''}".strip()]
        if d.get("attempt") is not None:
            parts.append(f"attempt {d.get('attempt')}")
        if d.get("waveBase"):
            parts.append(f"on {str(d.get('waveBase'))[:12]}")
        extra = [x for x in (
            f"contract rev {d.get('contractRevision')}" if d.get("contractRevision") is not None else "",
            f"prompt {str(d.get('promptSha256'))[:12]}" if d.get("promptSha256") else "",
            f"{len(d.get('lessons') or [])} lessons" if "lessons" in d else "",
            f"{len(d.get('directives') or [])} directives" if "directives" in d else "") if x]
        return (" ".join(parts) + (f" ({', '.join(extra)})" if extra else "")
                + (f"; rung {d.get('rung')}: {d.get('rungReason')}" if d.get("rung") is not None else ""))
    if kind == "swarm_report":
        return (f"agent: {'done' if d.get('done') else 'not done'}: {d.get('summary') or ''}"
                + (f" ({len(d.get('files') or [])} files)" if d.get("files") else "")
                + f"; review {'accepted' if d.get('accepted') else 'did not accept'}"
                + (f": {d.get('reviewNotes')}" if d.get("reviewNotes") else ""))
    if kind == "session":
        inj = d.get("feedbackInjections") or []
        return (f"session {d.get('candidate')}: delivered={d.get('delivered')} tests={d.get('testsPassed')} "
                f"${float(d.get('costUsd') or 0):.3f}, {len(inj)} feedback injection(s), "
                f"{len(d.get('checkpoints') or [])} checkpoint(s)"
                + (f", INTERRUPTED by {d['interruptedBy'].get('by')}" if d.get("interruptedBy") else "")
                + (f", PUSHBACK: {d['pushback'].get('reason')}" if d.get("pushback") else ""))
    if kind == "delivery":
        return f"integrated {str(d.get('base'))[:12]}..{str(d.get('head'))[:12]}"
    if kind == "review":
        return f"review {d.get('verdict')}" + (f": {'; '.join(d.get('findings') or [])}" if d.get("findings") else "")
    if kind == "directive":
        if d.get("op") == "lift":
            return f"lifted directive {d.get('lifts')} (by {d.get('by')})"
        if d.get("op") == "exempt":
            return f"{d.get('by')} exempted {d.get('item')} from directive {d.get('exempts')}: {d.get('reason') or ''}"
        return f"directive [{d.get('urgency') or 'advisory'}] from {d.get('by')}: {d.get('text')}"
    if kind == "lesson":
        if d.get("op") == "retract":
            return f"retracted lesson: {d.get('text')}"
        return f"lesson [{d.get('kind') or 'verified'}, {d.get('confidence')}]: {d.get('text')}"
    if kind == "exception":
        return f"exception {d.get('kind')} {'resolved' if d.get('resolved') else 'raised'}: {d.get('detail')}"
    if kind == "spend":
        return f"spend ${float(d.get('usd') or 0):.3f} ({d.get('author')})"
    if kind == "item":
        return f"defined: {d.get('title') or d.get('description') or d.get('id')}"
    return str({k: v for k, v in d.items() if k in ("decision", "outcome", "revision", "candidate", "paths")})


def item_timeline(correlation_id: str, item_id: str) -> list[dict]:
    """Every fact about one item, oldest first: ``[{ts, kind, author, summary, data}]``."""
    events = []
    for kind in _KINDS:
        for f in bb.read_facts(correlation_id, kind=kind):
            d = f.get("data") or {}
            if _about(kind, str(f.get("author") or ""), d, item_id):
                events.append({"ts": f.get("ts"), "kind": kind, "author": f.get("author"),
                               "summary": _summary(kind, d), "data": d})
    events.sort(key=lambda e: e["ts"] or 0)
    return events
