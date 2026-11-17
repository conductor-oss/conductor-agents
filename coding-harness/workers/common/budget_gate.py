"""Aggregate per-run spend gate (roadmap H5, Phase 0).

The existing caps (`maxTurns`, `maxBudgetUsd`) are **per-agent**. A swarm multiplies agents by
reviewers over days, so a run needs an **aggregate** ceiling it cannot exceed. This module:

  * records each agent's real-cost spend as a lock-free, append-only fact on the shared
    blackboard (per-author-unique keys — hundreds of concurrent writers, no lock,
    no lost update), pricing it from token usage via ``common.cost``;
  * folds those facts into a truthful run-level report (total, per-author, per-model, percent of
    ceiling);
  * decides ``ok | warn | halt`` purely from accumulated spend vs. the ceiling.

**This is a spend limit, never a wall-clock deadline** (``AGENTS.md`` timeout policy — turn/dollar
caps are agent limits, not execution deadlines). Nothing here aborts on elapsed time; the only cause
of a ``halt`` is cumulative spend reaching the ceiling. It makes **no** up-front cost estimate
(roadmap H5): the human approves an order-of-magnitude ceiling and the run proves or disproves it.

Pure logic only, so it is unit-testable; the thin ``@worker_task`` wrapper lives in
``campaign/tasks.py``.
"""

from __future__ import annotations

from common import cost
from common import blackboard as bb

SPEND_KIND = "spend"
DEFAULT_WARN_FRACTION = 0.8


def record_spend(correlation_id: str, author: str, usage: dict | None = None,
                 model: str | None = None, *, tokens: int | None = None,
                 usd: float | None = None, backend=None) -> dict:
    """Append this agent's spend to the board (lock-free, per-author-unique) and return the running
    run-level total. USD is derived from ``usage`` + ``model`` via ``common.cost`` unless given."""
    if usd is None:
        if usage:
            usd = cost.price_usage(usage, model)
        elif tokens:
            usd = cost.price_tokens(int(tokens), model)
        else:
            usd = 0.0
    if tokens is None:
        tokens = cost.usage_tokens(usage) if usage else 0
    bb.post_fact(correlation_id, author, SPEND_KIND,
                 {"usd": float(usd), "tokens": int(tokens), "model": model or "", "author": author},
                 backend=backend)
    return read_spend(correlation_id, backend=backend)


def read_spend(correlation_id: str, *, backend=None) -> dict:
    """Fold all spend facts into a truthful report: total USD/tokens, and breakdowns by author and
    by model. Assembled from the append-log, so it is an audit trail, not a mutable counter."""
    facts = bb.read_facts(correlation_id, kind=SPEND_KIND, backend=backend)
    total_usd = 0.0
    total_tokens = 0
    by_author: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    for f in facts:
        d = f.get("data") or {}
        usd = float(d.get("usd") or 0.0)
        tok = int(d.get("tokens") or 0)
        a = str(d.get("author") or f.get("author") or "anon")
        m = str(d.get("model") or "")
        total_usd += usd
        total_tokens += tok
        ba = by_author.setdefault(a, {"usd": 0.0, "tokens": 0})
        ba["usd"] = round(ba["usd"] + usd, 6)
        ba["tokens"] += tok
        bm = by_model.setdefault(m, {"usd": 0.0, "tokens": 0})
        bm["usd"] = round(bm["usd"] + usd, 6)
        bm["tokens"] += tok
    return {"usd": round(total_usd, 6), "tokens": total_tokens,
            "by_author": by_author, "by_model": by_model, "fact_count": len(facts)}


def by_role(by_author: dict) -> dict:
    """Group per-author spend by team role (implementer / reviewer / planner / tests / child team) so
    the report shows where the money went (H5: reviewers multiply cost — the report must show it)."""
    roles: dict[str, float] = {}
    for author, rec in (by_author or {}).items():
        a = str(author)
        role = ("implementer" if a.startswith("implementer:") else
                "reviewer" if a.startswith("review_") else
                "planner" if a.startswith("planner") else
                "tests" if a.startswith("tests") else
                "test_author" if a.startswith("acceptance:") else
                "child_team" if a.startswith("child:") else "other")
        roles[role] = round(roles.get(role, 0.0) + float((rec or {}).get("usd") or 0.0), 6)
    return roles


def gate(spend_usd: float, ceiling_usd: float | None, warn_fraction: float = DEFAULT_WARN_FRACTION) -> dict:
    """Pure decision from accumulated spend vs. the ceiling.

    Returns ``state`` one of ``ok`` | ``warn`` | ``halt`` | ``unbounded``. ``unbounded`` means no
    enforceable ceiling was configured — a state the caller must treat as not-startable for a
    swarm-mode run (see ``is_startable``). The halt is caused only by spend, never by time."""
    spend_usd = float(spend_usd or 0.0)
    if ceiling_usd is None or float(ceiling_usd) <= 0:
        return {"state": "unbounded", "enforceable": False, "spend_usd": round(spend_usd, 6),
                "ceiling_usd": None, "warn_at_usd": None, "fraction": None, "percent": None}
    ceiling_usd = float(ceiling_usd)
    warn_fraction = min(max(float(warn_fraction), 0.0), 1.0)
    warn_at = round(ceiling_usd * warn_fraction, 6)
    if spend_usd >= ceiling_usd:
        state = "halt"
    elif spend_usd >= warn_at:
        state = "warn"
    else:
        state = "ok"
    frac = spend_usd / ceiling_usd
    return {"state": state, "enforceable": True, "spend_usd": round(spend_usd, 6),
            "ceiling_usd": round(ceiling_usd, 6), "warn_at_usd": warn_at,
            "fraction": round(frac, 6), "percent": round(frac * 100, 2)}


def evaluate(correlation_id: str, ceiling_usd: float | None,
             warn_fraction: float = DEFAULT_WARN_FRACTION, *, backend=None) -> dict:
    """Read the board's accumulated spend and gate it, returning the decision plus a truthful report."""
    spend = read_spend(correlation_id, backend=backend)
    decision = gate(spend["usd"], ceiling_usd, warn_fraction)
    return {**decision,
            "report": {**spend, "ceiling_usd": decision["ceiling_usd"],
                       "warn_fraction": warn_fraction}}


def is_startable(ceiling_usd: float | None, *, swarm_mode: bool = True) -> bool:
    """A swarm-mode run MUST carry an enforceable ceiling (roadmap H5: a swarm that cannot stop
    itself on spend must not be startable). Single-agent mode keeps its per-agent caps and is
    unaffected by this rule."""
    if not swarm_mode:
        return True
    return ceiling_usd is not None and float(ceiling_usd) > 0
