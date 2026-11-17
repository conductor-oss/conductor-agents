"""The monitor's detection logic (roadmap Phase 3, §4.5) — construction-progress watching, pure.

A monitor runs concurrently with the team cycle and has **bounded authority** (§4.5, open question
#2): it may *nudge and re-prioritize* autonomously, but must *escalate* anything bigger (kill work,
expand scope, spend, unblock) to a human. "Monitor agents" here watch the *construction work in
progress* — stalls, drift, duplication, quality regression, budget overrun — not a running system.

This module is the pure fold: given a folded board (``team_board.fold_board``) plus the run's spend vs.
ceiling, it classifies each signal into an **autonomous action** (a directive/re-prioritize the
monitor applies itself) or an **exception** (an escalation for the human exception queue, H7). The
thin ``team_monitor`` worker applies the autonomous actions and posts the exceptions to the board.

Err toward escalation: when a signal could be either, it escalates.
"""

from __future__ import annotations

from common import team_board as tb


def _mapping(v):
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else {}


def _review_changes_by_item(reviews) -> dict:
    out: dict[str, int] = {}
    for r in reviews or []:
        r = _mapping(r)
        if str(r.get("verdict")) == "changes" and not r.get("scopeOnly"):
            iid = str(r.get("item"))
            out[iid] = out.get(iid, 0) + 1
    return out


def detect(board: dict, *, spend_usd: float = 0.0, ceiling_usd=None,
           stall_attempts: int = 2, quality_changes: int = 2, warn_fraction: float = 0.8,
           max_human_constraints: int = 3) -> dict:
    """Classify the board's health into autonomous actions + exceptions.

    Autonomous (the monitor applies): a **stall/thrash** (an item retried/reopened ``stall_attempts``+
    times) → re-prioritize it up and post a "try a different approach" directive; a **quality
    regression** (``quality_changes``+ peer-review ``changes`` rounds on one item) → a directive to
    address the specific findings, not re-attempt blindly.

    Exceptions (escalate to a human): a **blocked** item (terminal failure — unblocking is above the
    monitor's authority), **duplication** (two active items holding overlapping files — a
    decomposition defect), and a **budget breach** (spend at/over the ceiling — spend authority is the
    human's, H5). A budget *warning* (>= ``warn_fraction`` of the ceiling) is a non-escalating signal.
    """
    items = _mapping(board.get("items"))
    reviews = board.get("reviews") or []
    changes_by = _review_changes_by_item(reviews)

    actions, exceptions, signals = [], [], []

    for iid, it in items.items():
        state = str(it.get("state"))
        attempt = int(it.get("attempt") or 0)
        # Stall / thrash — autonomous nudge + bump (still open, retried a lot).
        if state in (tb.REOPENED, tb.IN_PROGRESS) and attempt >= stall_attempts:
            signals.append({"item": iid, "kind": "stall", "attempt": attempt})
            actions.append({"op": "nudge", "item": iid,
                            "text": (f"You have retried item '{iid}' {attempt} times without landing it. "
                                     f"Change approach: make the smallest possible edit that satisfies the "
                                     f"acceptance criteria, and do not reorder existing code.")})
            actions.append({"op": "reprioritize", "item": iid, "priority": 100 + attempt})
        # Quality regression — autonomous directive to address the findings.
        if changes_by.get(iid, 0) >= quality_changes and state in tb.OPEN_STATES:
            signals.append({"item": iid, "kind": "quality", "changes": changes_by[iid]})
            actions.append({"op": "nudge", "item": iid,
                            "text": (f"Peer review has requested changes on '{iid}' {changes_by[iid]} times. "
                                     f"Address each specific finding directly rather than re-implementing.")})
        # Blocked — escalate (unblocking is above monitor authority).
        if state == tb.BLOCKED:
            exceptions.append({"item": iid, "kind": "blocked", "severity": "high",
                               "detail": f"item '{iid}' is blocked: {it.get('reason') or 'terminal failure'}."})

    # Over-constraint (H16) — sticky human constraints must not silently deadlock the work. An item with
    # too many active human constraints, or one that keeps failing while it carries any, is flagged to a
    # human (lifting a constraint is above the monitor's authority — it escalates, never lifts).
    human_by_item: dict[str, list[str]] = {}
    for d in board.get("directives") or []:
        d = _mapping(d)
        if str(d.get("authority")) == "human" and str(d.get("target")) in items:
            human_by_item.setdefault(str(d.get("target")), []).append(str(d.get("text") or ""))
    for iid, texts in human_by_item.items():
        it = items[iid]
        stuck = int(it.get("attempt") or 0) >= stall_attempts and str(it.get("state")) in tb.OPEN_STATES | {tb.BLOCKED}
        if len(texts) >= max_human_constraints or (stuck and texts):
            exceptions.append({"item": iid, "kind": "over_constrained", "severity": "high",
                               "detail": (f"item '{iid}' carries {len(texts)} active human constraint(s)"
                                          + (f" and has failed {it.get('attempt')} attempt(s)" if stuck else "")
                                          + ": " + " | ".join(t[:120] for t in texts)
                                          + ". Review, lift or replace them — they may conflict.")})

    # Duplication — two active items holding overlapping files (a decomposition defect, H14).
    owners = _mapping(board.get("owners"))
    active = {iid: {str(f) for f in (it.get("files") or [])}
              for iid, it in items.items() if str(it.get("state")) in (tb.IN_PROGRESS, tb.REOPENED)}
    seen = list(active.items())
    for a in range(len(seen)):
        for b in range(a + 1, len(seen)):
            ia, fa = seen[a]
            ib, fb = seen[b]
            overlap = fa & fb
            if overlap:
                exceptions.append({"item": f"{ia},{ib}", "kind": "duplication", "severity": "high",
                                   "detail": f"items '{ia}' and '{ib}' are both active and share files "
                                             f"{sorted(overlap)} — a decomposition/ownership defect."})

    # Budget — warn signal below the ceiling; escalate at/over it (spend authority is the human's).
    if ceiling_usd not in (None, "") and float(ceiling_usd) > 0:
        ceiling = float(ceiling_usd)
        if spend_usd >= ceiling:
            exceptions.append({"item": "run", "kind": "budget", "severity": "high",
                               "detail": f"run spend ${spend_usd:.2f} reached the ceiling ${ceiling:.2f}; "
                                         f"the budget gate halts new work. A human must raise the ceiling or stop."})
        elif spend_usd >= warn_fraction * ceiling:
            signals.append({"item": "run", "kind": "budget_warn",
                            "detail": f"run spend ${spend_usd:.2f} is near the ceiling ${ceiling:.2f}."})

    return {"signals": signals, "autonomousActions": actions, "exceptions": exceptions,
            "rollup": tb.board_rollup(board)}
