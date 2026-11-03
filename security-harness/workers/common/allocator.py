"""The allocator — the swarm standup brain (design/SWARM.md sections 2-4, 7).

Pure logic (no IO): the worker reads the blackboard, calls these functions, and writes the results
back. Three jobs, run at each standup between rounds:

  * ``plan_wave``   -- size the next wave: spread the width ceiling across areas in proportion to
                       credit (follow the nose), so hot areas get more agents and dry areas starve.
  * ``reconcile``   -- fold the wave's facts back in: dedup, update coverage, boost/decay credit, and
                       SYNTHESIZE verified primitives into emergent chain-hypotheses (the hidden-bug
                       engine) via ``chaining.emergent_chains``.
  * ``should_continue`` -- the reflect/stop condition for the round loop.

Credit is the bandit signal: an area that yields a finding or a new verified primitive gains credit;
a dry wave decays it. Wave size is proportional to credit under the hard width ceiling (which is
itself under the native task-def throughput ceiling — this never raises real load).
"""

from __future__ import annotations

from common import chaining

SEED_CREDIT = 1.0      # a never-tried area starts worth one share
BOOST = 2.0            # added when an area yields a finding or a new primitive
DECAY = 0.5            # multiplier applied to a dry area's credit
FLOOR = 0.0


def _signature(h: dict) -> str:
    """The loop's dedupe signature for a hypothesis: category|target|identities."""
    cat = str(h.get("category") or "other").lower()
    tgt = str(h.get("target") or "").lower().replace(" ", "")
    ids = ",".join(sorted(str(i) for i in (h.get("identities") or [])))
    return f"{cat}|{tgt}|{ids}"


def score_areas(areas: list, credits: dict) -> list:
    """Attach a credit score to each area (seed credit for a never-seen/untested area)."""
    out = []
    for a in areas or []:
        if not isinstance(a, dict) or not a.get("area"):
            continue
        key = str(a["area"])
        c = credits.get(key)
        if c is None:
            c = SEED_CREDIT if a.get("untested", True) else SEED_CREDIT
        out.append({**a, "credit": max(FLOOR, float(c))})
    return out


# Default diversity pools so a wave is an ENSEMBLE, not clones (design/SWARM.md section 3): scouts in
# one wave vary across these so they search different framings. Personas bias the attacker lens;
# temperatures vary exploration. A caller may override via the ``diversity`` arg (e.g. a model pool).
_DEFAULT_PERSONAS = ("ordinary-user", "malicious-insider", "anonymous-outsider", "integration-partner")
_DEFAULT_TEMPS = (0.7, 1.0, 1.2)


def _diversify(assignment: dict, index: int, diversity: dict | None) -> dict:
    """Stamp a diverse (model, persona, temperature) tuple onto an assignment by rotating the pools,
    so no two agents in a wave share the same configuration over these dimensions."""
    diversity = diversity or {}
    models = diversity.get("models") or []
    personas = diversity.get("personas") or list(_DEFAULT_PERSONAS)
    temps = diversity.get("temperatures") or list(_DEFAULT_TEMPS)
    out = dict(assignment)
    if models:
        out["model"] = models[index % len(models)]
    out["persona"] = personas[index % len(personas)]
    out["temperature"] = temps[index % len(temps)]
    return out


def signature(h: dict) -> str:
    """Public dedupe signature for a hypothesis (category|target|identities)."""
    return _signature(h)


def is_chain(h: dict) -> bool:
    """A hypothesis is a chain (-> chainer) if it is an emergent/chained one, else an exploiter job."""
    return (str(h.get("category") or "").lower() == "chaining"
            or str(h.get("mandatory_kind") or "") in ("chain", "emergent_chain"))


def _scout_fill(areas: list, credits: dict, slots: int) -> list:
    """Spread ``slots`` scout agents across untested areas in proportion to credit (largest
    remainder). Returns scout assignments (breadth)."""
    scored = [a for a in score_areas(areas, credits) if a["credit"] > FLOOR]
    if not scored or slots <= 0:
        return []
    total = sum(a["credit"] for a in scored) or 1.0
    raw = [(a, slots * a["credit"] / total) for a in scored]
    counts = {id(a): int(frac) for a, frac in raw}
    assigned = sum(counts.values())
    for a, frac in sorted(raw, key=lambda t: (t[1] - int(t[1])), reverse=True):
        if assigned >= slots:
            break
        counts[id(a)] += 1
        assigned += 1
    out = []
    for a in scored:
        for _ in range(counts.get(id(a), 0)):
            out.append({"role": "scout", "area": a["area"],
                        "objective_id": a.get("objective_id") or a.get("objective") or "",
                        "identities": a.get("identities") or [],
                        "focus_directive": a.get("focus_directive") or ""})
    return out[:slots]


def plan_wave(areas: list, open_hypotheses: list | None = None, review_candidates: list | None = None,
              credits: dict | None = None, width_ceiling: int = 24,
              budget_left: int | None = None, diversity: dict | None = None) -> dict:
    """Plan the next wave as a MIX of roles over the width ceiling (design/SWARM.md section 2-4):

      * **refuters** -- one per confirmed-but-unreviewed finding (adversarial peer review; cheap,
        decisive), capped so they never crowd out exploration;
      * **exploiters / chainers** -- one per OPEN hypothesis, carrying the hypothesis payload (a
        chain hypothesis -> chainer, else exploiter) -- this is the depth that confirms bugs;
      * **scouts** -- fill the remaining width across untested areas by credit (breadth).

    Each assignment carries exactly what its agent needs: a ``hypothesis`` for exploiter/chainer, a
    ``candidate`` for refuter, or an ``area`` for a scout. Returns ``{assignments, width}``."""
    open_hypotheses = open_hypotheses or []
    review_candidates = review_candidates or []
    credits = credits or {}
    ceiling = max(0, int(width_ceiling))
    if budget_left is not None:
        ceiling = min(ceiling, max(0, int(budget_left)))
    if ceiling <= 0:
        return {"assignments": [], "width": 0}

    assignments: list[dict] = []
    # 1) Refuters -- bounded so review never starves discovery.
    ref_cap = max(1, ceiling // 3)
    for c in review_candidates[:min(ref_cap, ceiling)]:
        assignments.append({"role": "refuter", "candidate": c,
                            "area": str(c.get("area") or c.get("objective_id") or ""),
                            "objective_id": str(c.get("objective_id") or "")})
    # Reserve a SCOUT FLOOR so a hypothesis-rich wave can't fill 100% of the width with exploiters and
    # starve breadth/understanding (the scouts are also the agents that read the docs via search_docs).
    # Only reserve when there are untested areas to scout and room to spare.
    untested = [a for a in (areas or [])
                if bool((a.get("untested", True))) and str(a.get("area") or a.get("objective_id") or "")]
    # Scale breadth with the untested backlog: when many areas are unexplored, reserve up to HALF the
    # wave for scouts so depth can't starve breadth — the swarm must COVER the surface, not just
    # deepen a few leads (run 05b95dd9 gave breadth only 4/24 slots against 31 untested areas). As the
    # backlog shrinks round over round (now that reconcile marks worked areas scouted), the floor
    # falls and the wave shifts back toward depth. Floor of ceiling//6 keeps a few scouts always.
    if untested and ceiling - len(assignments) > 2:
        scout_floor = min(len(untested), max(ceiling // 6, min(ceiling // 2, len(untested))))
    else:
        scout_floor = 0
    exploiter_cap = max(0, ceiling - scout_floor)
    # 2) Exploiters / chainers -- one per open hypothesis, carrying the payload.
    for h in open_hypotheses:
        if len(assignments) >= exploiter_cap:
            break
        assignments.append({"role": "chainer" if is_chain(h) else "exploiter", "hypothesis": h,
                            "area": str(h.get("target") or h.get("objective_id") or ""),
                            "objective_id": str(h.get("objective_id") or ""),
                            "identities": h.get("identities") or [],
                            "focus_directive": str(h.get("title") or "")})
    # 3) Scouts fill the rest (including the reserved floor) on untested areas by credit.
    remaining = ceiling - len(assignments)
    if remaining > 0:
        assignments.extend(_scout_fill(areas or [], credits, remaining))

    assignments = [_diversify(a, i, diversity) for i, a in enumerate(assignments[:ceiling])]
    return {"assignments": assignments, "width": len(assignments)}


def reconcile(state: dict, new_facts: list, credits: dict | None = None,
              identity_labels: list | None = None) -> dict:
    """Fold a completed wave back into campaign state at the standup.

    Returns ``{tried_signatures, emergent_chains, credit_updates, coverage_updates, promoted}``.
    Dedups hypotheses, boosts/decays per-area credit by what each area yielded, and synthesizes the
    verified-primitive pool into emergent chain-hypotheses (deduped against everything tried)."""
    state = state or {}
    credits = dict(credits or {})
    facts = new_facts or []

    # Dedup emergent chains against EVERY hypothesis already on the board (open, retrying, or done),
    # so we never regenerate one that exists -- not just the confirmed/exhausted ones.
    tried = set(state.get("all_hypothesis_signatures") or state.get("tried_signatures") or [])
    for f in facts:
        d = (f or {}).get("data") or {}
        if (f or {}).get("kind") in ("hypothesis", "attempt") and d.get("signature"):
            tried.add(str(d["signature"]))

    # Per-area yield this round -> credit boost/decay.
    yielded: dict[str, bool] = {}
    coverage_updates: dict[str, dict] = {}
    promoted = []
    for f in facts:
        kind, d = (f or {}).get("kind"), (f or {}).get("data") or {}
        area = str(d.get("area") or d.get("objective_id") or "")
        if kind in ("finding", "verified_primitive") and (kind == "verified_primitive" or d.get("confirmed")):
            if area:
                yielded[area] = True
            if kind == "finding":
                promoted.append(d)
        if kind == "coverage" and area:
            coverage_updates[area] = {**coverage_updates.get(area, {}), **d}

    touched_areas = {str(d.get("area") or d.get("objective_id") or "")
                     for f in facts for d in [(f or {}).get("data") or {}] if (d.get("area") or d.get("objective_id"))}

    # HONEST COVERAGE: any seeded area that got WORKED this round (an attempt, deepen lesson,
    # observation, or scout) is no longer "untested" — mark it so the backlog shrinks, scouts rotate
    # to genuinely-unexplored areas, and the stop decision + report reflect real coverage. Without
    # this, reconcile only advanced coverage from explicit coverage facts (which scouts never post),
    # so run 05b95dd9 marked 2/31 areas tested while exploiters had actually worked 16 — the loop
    # then declared "dry" with 89% of the surface never hunted. An area that produced a confirmed
    # finding is marked exploited (stays hot); otherwise scouted. Never invents coverage rows from
    # free-text area strings — only updates areas already on the board.
    seeded_areas = set((state.get("coverage") or {}).keys())
    worked_areas = set()
    for f in facts:
        # Real work = an exploit attempt, a deepen lesson, or a scout observation tagged to the area.
        # NOT coverage facts (the seed coverage rows are status=untested and must not self-certify).
        if (f or {}).get("kind") in ("attempt", "attempt_lesson", "observation"):
            d = (f or {}).get("data") or {}
            aid = str(d.get("objective_id") or d.get("area") or "")
            if aid in seeded_areas:
                worked_areas.add(aid)
    # An area that produced a confirmed finding this round was unquestionably worked (exploited).
    worked_areas |= {a for a in yielded if a in seeded_areas}
    for area in worked_areas:
        prev = coverage_updates.get(area, {})
        prev_status = prev.get("status")
        status = "exploited" if yielded.get(area) else (
            prev_status if prev_status in ("exploited", "scouted") else "scouted")
        coverage_updates[area] = {**prev, "area": area, "status": status, "untested": False}
    credit_updates = dict(credits)
    for area in touched_areas:
        base = float(credit_updates.get(area, SEED_CREDIT))
        credit_updates[area] = base + BOOST if yielded.get(area) else max(FLOOR, base * DECAY)

    # SYNTHESIS: compose the verified-primitive pool into emergent chains (the hidden-bug engine).
    primitives = list(state.get("verified_primitives") or [])
    for f in facts:
        if (f or {}).get("kind") == "verified_primitive":
            primitives.append((f or {}).get("data") or {})
    emergent = chaining.emergent_chains(primitives, identity_labels=identity_labels,
                                        tried_signatures=sorted(tried))

    return {
        "tried_signatures": sorted(tried),
        "emergent_chains": emergent,
        "credit_updates": credit_updates,
        "coverage_updates": coverage_updates,
        "promoted": promoted,
    }


def work_remaining(state: dict) -> dict:
    """Count the REAL work left on the board after a round: open hypotheses (including retries),
    confirmed findings awaiting peer review, and untested areas. This is the honest dryness signal
    — the loop keeps deepening while any of these is non-zero, not merely because some fact was
    posted."""
    state = state or {}
    untested = sum(1 for a in (state.get("coverage") or {}).values()
                   if (a or {}).get("untested", False) is True)
    return {
        "open_hypotheses": len(state.get("open_hypotheses") or []),
        "review_candidates": len(state.get("review_candidates") or []),
        "untested_areas": untested,
        "total": len(state.get("open_hypotheses") or []) + len(state.get("review_candidates") or []) + untested,
    }


def should_continue(state: dict, *, round_idx: int, max_rounds: int,
                    budget_left: int | None, made_progress: bool = False) -> dict:
    """Reflect/stop. Hard limits: round cap and budget. Otherwise keep looping while real work
    remains (open hypotheses/retries, review candidates, untested areas) OR we made progress this
    round (a new confirmation/primitive can seed chains next round). Stop only when genuinely dry —
    this is what makes it an N-loop deepening search rather than a one-shot."""
    if round_idx >= max_rounds:
        return {"continue": False, "reason": f"round cap reached ({round_idx} >= {max_rounds})"}
    if budget_left is not None and budget_left <= 0:
        return {"continue": False, "reason": "budget exhausted"}
    wr = work_remaining(state)
    if wr["total"] > 0 or made_progress:
        bits = []
        if wr["open_hypotheses"]:
            bits.append(f"{wr['open_hypotheses']} open/retry hypotheses")
        if wr["review_candidates"]:
            bits.append(f"{wr['review_candidates']} findings to peer-review")
        if wr["untested_areas"]:
            bits.append(f"{wr['untested_areas']} untested areas")
        if made_progress and not bits:
            bits.append("made progress (new confirmation/primitive) — one more round to chain")
        return {"continue": True, "reason": "; ".join(bits)}
    return {"continue": False, "reason": "dry: no open hypotheses, no review candidates, no untested areas"}
